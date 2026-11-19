#!/usr/bin/env python3
"""Logging reverse proxy between VS Code and a local model server. Standard library only.

  python3 -I copilot_tap.py --listen 127.0.0.1:8081 --upstream http://127.0.0.1:8000 --log tap.jsonl
  python3 -I copilot_tap.py report tap.jsonl

One JSONL record per /chat/completions request: sizes and hashes only, never prompt text, response
text or API keys. Both ends must be loopback. Hop-by-hop headers are dropped; all other headers and
the body pass through unchanged, and response bytes are forwarded as they arrive.
"""
import argparse, hashlib, http.client, http.server, itertools, json, os, re, select, socket, statistics, sys, threading, time
from urllib.parse import urlsplit

LOOPBACK = ("127.0.0.1", "localhost")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "expect"}
ROLES = {"system", "developer", "user", "assistant", "tool", "function"}
THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)
# A non-empty value. oMLX's keepalive chunks carry "content":"", so the first byte says nothing about prefill.
FIRST_TOKEN = re.compile(rb'"(?:reasoning_content|reasoning|content|arguments)"\s*:\s*"[^"]')
LOCK = threading.Lock()
PREVIOUS = {}  # model -> canonical text of its last request, in memory only

def sized(name, chars):
    return {f"{name}_chars": chars, f"{name}_tokens_est": round(chars / 4.1)}

def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

def text_of(content):
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""

def describe_request(body):
    req = json.loads(body)
    messages, tools = req.get("messages") or [], req.get("tools") or []
    system = "".join(text_of(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))
    tools_text, rest = canon(tools), canon([m for m in messages if m.get("role") not in ("system", "developer")])
    roles = [m.get("role") if m.get("role") in ROLES else "other" for m in messages]
    runs = [(r, len(list(g))) for r, g in itertools.groupby(roles)]
    model, whole = str(req.get("model")), tools_text + canon(messages)  # tools first: templates render them ahead of the messages
    with LOCK:
        old, PREVIOUS[model] = PREVIOUS.get(model), whole
    prefix = None if old is None else len(os.path.commonprefix([old, whole]))
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model, "stream": bool(req.get("stream")), "messages": len(messages),
            "roles": " ".join(f"{r}*{n}" if n > 1 else r for r, n in runs)[-400:], "tools": len(tools),
            **sized("system", len(system)), **sized("tools", len(tools_text)), **sized("messages", len(rest)),
            "prompt_tokens_est": round((len(system) + len(tools_text) + len(rest)) / 4.1),
            "tools_hash": digest(tools_text), "system_hash": digest(system), "prefix_chars": prefix,
            "prefix_share": None if old is None else round(prefix / max(len(whole), 1), 4),
            "prefix_of_previous": None if old is None else round(prefix / max(len(old), 1), 4),
            "reasoning_echoed": any(m.get("role") == "assistant" and (m.get("reasoning_content") or m.get("reasoning")) for m in messages),
            "reasoning_effort": req.get("reasoning_effort"), "temperature": req.get("temperature"),
            "max_tokens": req.get("max_tokens", req.get("max_completion_tokens"))}

def numbers_only(usage):
    return {k: numbers_only(v) if isinstance(v, dict) else v for k, v in usage.items() if isinstance(v, (int, float, dict))}

def describe_reply(raw, sse):
    """Finish reason, usage and sizes of a response body. The text is counted, never kept."""
    think = args = 0
    texts, finish, usage, error = [], None, None, None
    for chunk in raw.split(b"\n") if sse else [raw]:
        if sse and not chunk.startswith(b"data:"):  # comment frames and blank lines
            continue
        try:
            obj = json.loads(chunk[5:] if sse else chunk)  # also skips oMLX's leading keepalive spaces
            usage = obj.get("usage") or usage
            error = "error_in_body" if obj.get("error") else error
            for choice in (obj.get("choices") or [])[:1]:
                finish = choice.get("finish_reason") or finish
                d = choice.get("delta") or choice.get("message") or {}
                think += len(d.get("reasoning_content") or d.get("reasoning") or "")
                args += sum(len(str((c.get("function") or {}).get("arguments") or "")) for c in d.get("tool_calls") or [])
                texts.append(text_of(d.get("content")))
        except (ValueError, TypeError, AttributeError):
            pass  # [DONE], or a frame that is not ours to understand; the bytes were forwarded anyway
    text = "".join(texts)
    answer = THINK.sub("", text)
    usage = numbers_only(usage) if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or {}
    return {"finish_reason": str(finish)[:32] if finish else None, "usage": usage or None, "body_error": error,
            "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))),
            **sized("thinking", think + len(text) - len(answer)), **sized("answer", len(answer)), **sized("tool_call", args)}

class Tap(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # an SSE frame must not wait for more data
    up = log = None
    log_message = lambda self, *args: None

    def __getattr__(self, name):  # forward every HTTP method
        if name.startswith("do_"):
            return self.proxy
        raise AttributeError(name)

    def read_body(self):
        if "chunked" not in self.headers.get("Transfer-Encoding", "").lower():
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        parts, size = [], 1
        while size:
            size = int(self.rfile.readline().split(b";")[0], 16)
            parts.append(self.rfile.read(size + 2)[:size])  # +2 for the CRLF after each chunk
        return b"".join(parts)

    def watch_client(self, upstream_sock, done, gone):
        """Cut upstream as soon as the client hangs up, even while upstream is silent."""
        try:
            while not done.is_set():
                if select.select([self.connection], [], [], 0.5)[0]:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        gone.set()
                        upstream_sock.shutdown(socket.SHUT_RDWR)
                    return
        except (OSError, ValueError):
            pass

    def proxy(self):
        body, started, record = self.read_body(), time.time(), None
        if self.command == "POST" and urlsplit(self.path).path.endswith("/chat/completions"):
            try:
                record = describe_request(body)
            except Exception:  # forwarding must not depend on understanding the body
                record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "error": "unreadable_request"}
        conn = http.client.HTTPConnection(self.up.hostname, self.up.port)  # no timeout: a long prefill is normal
        done, gone = threading.Event(), threading.Event()
        raw, resp, first_byte, first_token, error, where = [], None, None, None, None, "upstream"
        try:
            conn.connect()
            threading.Thread(target=self.watch_client, args=(conn.sock, done, gone), daemon=True).start()
            conn.putrequest(self.command, self.up.path.rstrip("/") + self.path, skip_host=True, skip_accept_encoding=True)
            for key, value in self.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    conn.putheader(key, value)
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                conn.putheader("Content-Length", str(len(body)))  # read_body decoded it
            conn.endheaders(body)
            resp = conn.getresponse()
            no_body = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
            rechunk = resp.chunked and not no_body and self.request_version == "HTTP/1.1"
            where = "client"
            self.send_response_only(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            if rechunk:
                self.send_header("Transfer-Encoding", "chunked")
            elif resp.length is None and not no_body:
                self.send_header("Connection", "close")  # the body ends when this connection closes
            self.end_headers()
            while not no_body:
                where = "upstream"
                data = resp.read1(65536)  # returns as soon as any bytes arrive
                if not data:
                    break
                first_byte = first_byte or time.time() - started
                if record is not None:
                    raw.append(data)
                    if first_token is None and FIRST_TOKEN.search(data):
                        first_token = time.time() - started
                where = "client"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if rechunk else data)
            if gone.is_set() or resp.length:
                raise ConnectionError("short body")  # the client must see it as short too
            if rechunk:
                self.wfile.write(b"0\r\n\r\n")
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True
            error = "client_gone" if gone.is_set() or where == "client" else "upstream_dropped" if resp else "upstream_unreachable"
            if not resp and not gone.is_set():
                self.send_error(502, "copilot_tap: upstream unreachable")
        finally:
            done.set()
            if resp:
                resp.close()  # conn.close() alone leaves the socket open once it has passed to the response
            conn.close()
            if record is not None:
                self.write_record(record, resp, raw, error, started, first_byte, first_token)

    def write_record(self, record, resp, raw, error, started, first_byte, first_token):
        sse = bool(resp and "event-stream" in (resp.getheader("Content-Type") or ""))
        encoded = bool(resp and resp.getheader("Content-Encoding"))  # a compressed body cannot be read; its sizes stay zero
        record.update(describe_reply(b"" if encoded else b"".join(raw), sse), body_encoded=encoded, status=resp.status if resp else 502,
                      total_s=round(time.time() - started, 3), first_byte_s=first_byte and round(first_byte, 3),
                      first_token_s=first_token and round(first_token, 3))
        record["error"] = record.get("error") or error or record.get("body_error")
        del record["body_error"]
        with LOCK:
            self.log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log.flush()

def report(path):
    by_model = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.endswith("}\n"):  # a tap killed mid-write can leave a partial last line
                r = json.loads(line)
                by_model.setdefault(str(r.get("model")), []).append(r)
    rows = [["model", "reqs", "prompt_med", "prompt_max", "first_byte_med", "first_token_med", "total_med", "prefix<50%",
             "tools_n_changes", "tools_hash_changes", "reasoning_echoed", "thinking_tok_med"]]
    for model, rs in sorted(by_model.items()):
        col = lambda key: [r[key] for r in rs if r.get(key) is not None]
        med = lambda key: round(statistics.median(col(key)), 1) if col(key) else "-"
        flips = lambda key: sum(a != b for a, b in zip(col(key), col(key)[1:]))
        replied = [r for r in rs if "assistant" in (r.get("roles") or "")]  # only these can echo reasoning
        rows.append([model, len(rs), med("prompt_tokens_est"), max(col("prompt_tokens_est"), default="-"), med("first_byte_s"),
                     med("first_token_s"), med("total_s"), sum(s < 0.5 for s in col("prefix_share")), flips("tools"), flips("tools_hash"),
                     f"{sum(bool(r.get('reasoning_echoed')) for r in replied)}/{len(replied)}", med("thinking_tokens_est")])
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())

def main():
    if sys.argv[1:2] == ["report"] and len(sys.argv) == 3:
        return report(sys.argv[2])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("--listen", "--upstream", "--log"):
        ap.add_argument(flag, required=True)
    args = ap.parse_args()
    listen, Tap.up = urlsplit("//" + args.listen), urlsplit(args.upstream)
    if listen.hostname not in LOOPBACK or listen.port is None:
        sys.exit(f"copilot_tap: refusing --listen {args.listen}: must be 127.0.0.1:PORT or localhost:PORT")
    if Tap.up.scheme != "http" or Tap.up.hostname not in LOOPBACK:
        sys.exit(f"copilot_tap: refusing --upstream {args.upstream}: must be http:// on loopback")
    Tap.log = os.fdopen(os.open(args.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8")
    server = http.server.ThreadingHTTPServer((listen.hostname, listen.port), Tap)
    server.handle_error = lambda *a: None  # a traceback could quote part of a request
    print(f"copilot_tap: listening on {server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
