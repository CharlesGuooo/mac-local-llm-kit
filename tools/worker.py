#!/usr/bin/env python3
"""run_local_task: one bounded job for a local model in a throwaway git worktree. Standard library only.

  python3 -I worker.py --init
  python3 -I worker.py --task-file t.txt --files a.py,b.py --mode edit --edit-paths a.py --gate unit --model qwen36
  python3 -I worker.py --task-file t.txt --files a.py --mode read
  python3 -I worker.py --mcp --repo /path/to/repo

The repository comes from --repo, or else from the current directory, so an MCP client must pass one or the other.
Model text is never executed; the gate does run the edited code, so a gate is no sandbox.
Exit codes: 0 pass, 10 gate_fail, 11 apply_fail, 12 refused, 13 timeout, 14 model_error, 2 bad command line.
"""
import argparse, contextlib, fcntl, fnmatch, http.client, json, os, re, shlex, shutil, signal, socket, subprocess, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

EXIT = {"pass": 0, "gate_fail": 10, "apply_fail": 11, "refused": 12, "timeout": 13, "model_error": 14}
HARD_LIMIT_S, MAX_CHANGED_LINES, MAX_PAYLOAD_TOKENS, GATE_TAIL_CHARS = 900, 400, 12000, 2000
GATE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "TERM")
# After gh-aw's protected files: manifests, lockfiles, agent instructions, CI. Matched on the lowercased file name.
PROTECTED = ("agents.md", "claude.md", "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "pipfile",
             "go.mod", "go.sum", "cargo.toml", "pom.xml", "build.gradle*", "gemfile", "composer.json", "*.gemspec", "*.csproj",
             "*.lock", "*-lock.json", "*-lock.yaml", "jenkinsfile", "azure-pipelines.yml", "makefile")
SYSTEM = """You are a careful code worker. Use only the files given; do not guess at others.
To change a file, reply with one block per change, exactly in this form:
FILE: path
<<<<<<< SEARCH
existing lines, copied exactly, matching one place
=======
replacement lines
>>>>>>> REPLACE
An empty SEARCH creates a new file. Edit only the allowed paths. For a read-only task, just answer.
End with one line: NOTE: what you did, or why you could not."""
TOOL = {"name": "run_local_task", "description": "Run a bounded task on the local model in a throwaway git worktree. "
        "Returns a path, a diffstat and pass/fail; the patch stays on disk.",
        "inputSchema": {"type": "object", "required": ["task", "files", "mode"], "properties": {
            "task": {"type": "string", "description": "One paragraph"}, "mode": {"type": "string", "enum": ["read", "edit"]},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative files to give the model"},
            "edit_paths": {"type": "array", "items": {"type": "string"}, "description": "Subset of files allowed to change; default none"},
            "gate": {"type": "string", "description": "Name from .worker/gates.json; required for edit"},
            "model": {"type": "string", "enum": ["qwen36", "qwen38"], "description": "qwen36 is faster, qwen38 is stronger"}}}}
STARTERS = {"config.json": {"base_url": "http://127.0.0.1:8000/v1", "api_key_file": "~/.omlx_api_key", "models": {
    "qwen36": {"id": "Qwen3.6-35B-A3B-4bit", "max_tokens": 2048, "time_box_seconds": 270, "extra_body": {"chat_template_kwargs": {"preserve_thinking": True}}},
    "qwen38": {"id": "Qwen3.8-27B-oQ4e-mtp", "max_tokens": 2048, "time_box_seconds": 270, "extra_body": {"reasoning_effort": "low"}}}},
    "gates.json": {"unit": {"argv": ["python3", "-m", "pytest", "-q"], "timeout": 120}}}
BLOCK = re.compile(r"^FILE: *`?([^`\n]+?)`? *\n(?:```[^\n]*\n)?<<<<<<< SEARCH[ \t]*\n(.*?)^=======[ \t]*\n(.*?)^>>>>>>> REPLACE[ \t]*$", re.M | re.S)
THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)

class Stop(Exception):
    """Ends the job: Stop(status) or Stop(status, reason). A reason never quotes model text."""

def need(ok, why, status="refused"):
    if not ok:
        raise Stop(status, why)

def read_text(path):
    return Path(path).read_bytes().decode("utf-8")  # bytes, so CRLF files survive

def write_text(path, text):
    Path(path).write_bytes(text.encode("utf-8", "replace"))

def check(job):
    if job.cancel.is_set() or time.time() >= job.deadline:
        raise Stop("timeout", "cancelled" if job.cancel.is_set() else "time box")

def watch(job):
    """Heartbeat, and cut the model socket at the deadline or on cancel, so a silent server cannot hold the job."""
    every = job.spec.get("heartbeat_seconds", 60)
    next_beat = job.start + every
    while not job.ended.wait(0.2):
        if time.time() >= next_beat:
            job.beat(f"worker: {time.time() - job.start:.0f}s")
            next_beat += every
        if job.sock and (time.time() >= job.deadline or job.cancel.is_set()):
            with contextlib.suppress(OSError):
                job.sock.shutdown(socket.SHUT_RDWR)
            job.sock = None

def git(cwd, *args, must=True):
    """Hooks off, so nothing in the repo or the user's git config runs because of the worker."""
    r = subprocess.run(["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args], capture_output=True)
    need(r.returncode == 0 or not must, f"git {args[0]} failed")
    return r

def protected(path):
    parts = path.lower().split("/")  # lowercased: macOS file systems usually ignore case
    return parts[0].startswith(".") or ".git" in parts or ".worker" in parts or any(fnmatch.fnmatchcase(parts[-1], g) for g in PROTECTED)

def check_path(repo, path, may_be_new=False):
    need(path and isinstance(path, str) and "\0" not in path and not os.path.isabs(path)
         and os.path.normpath(path) == path and path.split("/")[0] != "..", f"bad path {path!r}")
    need(not protected(path), f"protected path {path}")
    full = os.path.join(repo, path)
    need(os.path.realpath(full).startswith(repo + os.sep), f"path escapes repo: {path}")
    tracked = git(repo, "ls-files", "--error-unmatch", "--", path, must=False).returncode == 0
    need(tracked or (may_be_new and not os.path.lexists(full)), f"not tracked: {path}")

def load_config(repo, model):
    cfg = json.loads(read_text(os.path.join(repo, ".worker", "config.json")))
    gates = json.loads(read_text(os.path.join(repo, ".worker", "gates.json")))
    spec, url = cfg["models"][model], urlsplit(cfg["base_url"])
    key_file = os.path.join(repo, os.path.expanduser(cfg["api_key_file"]))  # a relative path means relative to the repo
    need(not os.stat(key_file).st_mode & 0o077, "api_key_file must be mode 0600")
    need(read_text(key_file).strip(), "api_key_file is empty")
    need(url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost"), "base_url must be http:// on loopback")
    spec = dict({"max_tokens": 2048, "time_box_seconds": 270}, **spec)
    return spec, url, read_text(key_file).strip(), gates

def build_messages(repo, files, edit_paths, mode, task, limit):
    """Constant system text, files in sorted order, task last: no date or varying path, so the prefix can be reused."""
    parts = [f'<file path="{p}">\n{read_text(os.path.join(repo, p))}\n</file>\n' for p in files]
    user = "".join(parts) + (f"Allowed edits: {', '.join(edit_paths)}" if mode == "edit" else "Read-only.") + f"\nTask: {task}"
    tokens = int((len(SYSTEM) + len(user)) / 4.1)
    need(tokens <= limit, f"payload ~{tokens} tokens over budget {limit}; split the job")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

def chat(job, messages):
    """One streaming call. Returns (assistant message for a retry, answer text without thinking)."""
    check(job)
    job.attempts += 1
    extra = {k: v for k, v in (job.spec.get("extra_body") or {}).items() if k not in ("tools", "tool_choice")}
    body = dict(extra, model=job.spec["id"], messages=messages, stream=True, max_tokens=job.spec["max_tokens"],
                stream_options={"include_usage": True})
    conn = http.client.HTTPConnection(job.url.hostname, job.url.port, timeout=10)
    resp, started, first, finish, usage, text, think = None, time.time(), None, None, {}, "", ""
    try:
        conn.connect()
        conn.sock.settimeout(None)  # a long prefill is normal; only the time box ends it
        job.sock = conn.sock
        conn.request("POST", job.url.path.rstrip("/") + "/chat/completions", json.dumps(body),
                     {"Authorization": f"Bearer {job.key}", "Content-Type": "application/json", "Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:  # the server's reason, such as an unknown model id, stays on disk
            Path(job.last, "error.txt").write_bytes(resp.read(4000))
            raise Stop("model_error", f"HTTP {resp.status}, see error.txt")
        for raw in resp:
            if raw[5:].strip() == b"[DONE]":
                finish = finish or "stop"
                break
            obj = json.loads(raw[5:]) if raw.startswith(b"data:") else {}  # comment frames and blank lines fall out here
            # oMLX reports a late failure as a data frame, then [DONE].
            need(not obj.get("error"), "server reported an error in the stream", "model_error")
            usage = obj.get("usage") or usage
            for choice in (obj.get("choices") or [])[:1]:
                finish, delta = choice.get("finish_reason") or finish, choice.get("delta") or {}
                text += delta.get("content") or ""
                think += delta.get("reasoning_content") or delta.get("reasoning") or ""
                if first is None and (text or think):  # oMLX's default keepalive is an empty chunk, not a token
                    first = time.time()
        check(job)
        need(finish, "stream ended early", "model_error")
    except (OSError, ValueError, http.client.HTTPException):
        check(job)
        raise Stop("model_error", "stream broke" if resp else "cannot reach model server")
    finally:
        job.sock = None
        if resp:
            resp.close()  # conn.close() alone leaves the socket open once it has passed to the response
        conn.close()
    now = time.time()
    job.prefill, job.decode = job.prefill + (first or now) - started, job.decode + now - (first or now)
    job.tokens_in += usage.get("prompt_tokens") or int(sum(len(str(m["content"])) for m in messages) / 4.1)
    job.tokens_out += usage.get("completion_tokens") or int((len(text) + len(think)) / 4.1)
    write_text(os.path.join(job.last, f"answer-{job.attempts}.txt"), f"{think}\n---\n{text}")
    # A cut-off answer could still apply and pass while unfinished.
    need(finish != "length", f"cut off at max_tokens {job.spec['max_tokens']}", "model_error")
    # Qwen3.8's template re-renders reasoning_content; dropping it breaks the cache prefix at this turn.
    reply = dict({"role": "assistant", "content": text}, **({"reasoning_content": think} if think else {}))
    return reply, THINK.sub("", text).split("</think>", 1)[-1]  # if the template opened the block, only its close shows

def one_line(text):
    """Model text reaches the manager only like this: it cannot start a new line or send a control character."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())

def note_of(answer):
    notes = re.findall(r"^NOTE:(.*)$", BLOCK.sub("", answer), re.M)
    return "[untrusted] " + one_line(notes[-1])[:200] if notes else "-"

def apply_edits(wt, answer, edit_paths):
    """All blocks or none. edit_paths never holds a protected path, so this also rejects edits to .worker/."""
    blocks, new = BLOCK.findall(answer), {}
    need(blocks, "no edit blocks in the answer", "apply_fail")
    for path, search, replace in blocks:
        full = os.path.join(wt, path)
        need(path in edit_paths and not os.path.islink(full), "edit outside edit_paths", "apply_fail")
        text = new[path] if path in new else read_text(full) if os.path.exists(full) else ""
        need(text.count(search) == 1 if search else not text, f"SEARCH must match exactly one place in {path}", "apply_fail")
        new[path] = text.replace(search, replace) if search else replace
    for path, text in new.items():
        os.makedirs(os.path.dirname(os.path.join(wt, path)), exist_ok=True)
        write_text(os.path.join(wt, path), text)

def make_patch(job, edit_paths):
    diff = ("diff", "--cached", "--no-renames")
    git(job.wt, "add", "-A")
    names = [n for n in os.fsdecode(git(job.wt, *diff, "--name-only", "-z").stdout).split("\0") if n]
    need(names, "the edits changed nothing", "apply_fail")
    # Prefixes are explicit: a user's diff.noprefix or diff.mnemonicPrefix would make the patch fail to apply.
    patch = git(job.wt, *diff, "--binary", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/").stdout
    Path(job.last, "patch.diff").write_bytes(patch)
    need(all(n in edit_paths and not protected(n) for n in names), "real diff touches a path outside edit_paths")
    stats = [line.split(b"\t") for line in git(job.wt, *diff, "--numstat").stdout.splitlines()]
    plus, minus = (sum(int(s[i]) for s in stats if s[i] != b"-") for i in (0, 1))
    need(plus + minus <= MAX_CHANGED_LINES, f"diff has {plus + minus} changed lines, cap {MAX_CHANGED_LINES}")
    return f"{len(names)} files, +{plus} -{minus}"

def run_gate(job, out, name, gate):
    """Sets the gate: line, returns (passed, output tail). Scrubbed env; the whole process group dies at the end."""
    started, log_path = time.time(), os.path.join(job.last, "gate.log")
    limit = min(gate.get("timeout", 120), job.deadline - started)
    env = {k: os.environ[k] for k in GATE_ENV if k in os.environ}
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(gate["argv"], cwd=job.wt, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        raise Stop("refused", f"gate {name} cannot start")
    try:
        while proc.poll() is None and time.time() - started < limit and not job.cancel.is_set():
            time.sleep(0.05)
        timed_out = proc.poll() is None
    finally:  # also on Ctrl-C, which never reaches the gate's own session; and takes anything the gate left running
        with contextlib.suppress(OSError):  # macOS says EPERM when only zombies are left
            os.killpg(proc.pid, signal.SIGKILL)
    passed = proc.wait() == 0 and not timed_out
    out["gate"] = f"{name}  {'timeout' if timed_out else 'pass' if passed else 'fail'}  {time.time() - started:.1f}s"
    if timed_out:
        check(job)  # the time box cut it, rather than the gate's own timeout
    return passed, Path(log_path).read_bytes().decode("utf-8", "replace")[-GATE_TAIL_CHARS:]

def edit_job(job, out, messages, edit_paths, gate_name, gate):
    shutil.rmtree(job.wt, ignore_errors=True)
    git(job.repo, "worktree", "add", "--force", "--detach", job.wt, "HEAD")
    out["patch"] = os.path.join(job.last, "patch.diff")
    for attempt in (1, 2):
        reply, answer = chat(job, messages)
        out["note"] = note_of(answer)
        try:
            apply_edits(job.wt, answer, edit_paths)
            out["diffstat"] = make_patch(job, edit_paths)
        except Stop as stop:
            if stop.args[0] != "apply_fail" or attempt == 2:
                raise
            feedback = f"The edit failed: {stop.args[1]}. Nothing was applied. Send all the blocks again."
        else:
            passed, tail = run_gate(job, out, gate_name, gate)
            if passed:
                out["apply"] = f"git -C {shlex.quote(job.repo)} apply {shlex.quote(out['patch'])}"
                return
            need(attempt == 1 and time.time() < job.deadline, "", "gate_fail")
            git(job.wt, "checkout", "--", ".")  # back to what git add recorded: gate leftovers stay out of the next diff
            git(job.wt, "clean", "-fdq")
            feedback = (f"The gate failed. Last output:\n{tail}\n"
                        "Your edits are in place. Send only further blocks against the current files.")
        messages = messages + [reply, {"role": "user", "content": feedback}]

def render(out):
    return "\n".join(f"{k}: {v}" for k, v in out.items())

def run_job(repo, task, files, mode, edit_paths=None, gate=None, model=None, beat=lambda line: None, cancel=None):
    out = dict.fromkeys(("status", "job", "patch" if mode == "edit" else "result", "diffstat", "gate", "time", "note", "apply"), "-")
    lock = job = None
    try:
        need(mode in ("read", "edit") and task and isinstance(task, str) and files and isinstance(files, list),
             "need task, files and mode read or edit")
        spec, url, key, gates = load_config(repo, model or "qwen36")
        files, edit_paths = sorted(set(files)), sorted(set(edit_paths or [])) if mode == "edit" else []
        for path in files:
            check_path(repo, path)
        if mode == "edit":
            need(edit_paths and gate in gates, "edit mode needs edit_paths and a gate named in .worker/gates.json")
            for path in edit_paths:
                check_path(repo, path, may_be_new=path not in files)
            # The worktree starts at HEAD: an uncommitted named file would be stale for the model and for the patch.
            need(not git(repo, "status", "--porcelain", "--", *files, *edit_paths).stdout, "uncommitted named files; commit them first")
        box = min(spec["time_box_seconds"], HARD_LIMIT_S)
        # By default prefill at 90 tok/s gets at most half the box. A fast model can set its own limit.
        limit = spec.get("max_payload_tokens") or min(MAX_PAYLOAD_TOKENS, int(box * 45))
        messages = build_messages(repo, files, edit_paths, mode, task, limit)
        lock = open(os.path.join(repo, ".worker", "lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Stop("refused", "busy")
        now = time.time()
        job = SimpleNamespace(repo=repo, spec=spec, url=url, key=key, beat=beat, cancel=cancel or threading.Event(), start=now,
                              deadline=now + box, last=os.path.join(repo, ".worker", "last"), wt=os.path.join(repo, ".worker", "wt"),
                              sock=None, ended=threading.Event(), prefill=0, decode=0, tokens_in=0, tokens_out=0, attempts=0)
        threading.Thread(target=watch, args=(job,), daemon=True).start()
        shutil.rmtree(job.last, ignore_errors=True)
        os.makedirs(job.last)
        out["job"] = job.last
        if mode == "edit":
            edit_job(job, out, messages, edit_paths, gate, gates[gate])
        else:
            out["result"] = os.path.join(job.last, "result.md")
            answer = chat(job, messages)[1]
            write_text(out["result"], answer)
            out["note"] = note_of(answer)
            # The character cap is for text without spaces, such as Chinese.
            out["summary"] = "[untrusted] " + " ".join(one_line(re.sub(r"^NOTE:.*$", "", answer, flags=re.M)).split()[:150])[:1000]
        out["status"] = "pass"
    except Stop as stop:
        out["status"] = one_line(": ".join(a for a in stop.args if a))[:160]
    except Exception as e:  # the block always comes back: an MCP client would otherwise wait forever
        out["status"] = f"model_error: internal {type(e).__name__}" if job else one_line(f"refused: {type(e).__name__}: {e}")[:160]
    if job:
        job.ended.set()
        out["time"] = (f"prefill {job.prefill:.0f}s, decode {job.decode:.0f}s, total {time.time() - job.start:.0f}s; "
                       f"tokens {job.tokens_in} in, {job.tokens_out} out, attempts {job.attempts}")
        write_text(os.path.join(job.last, "block.txt"), render(out))  # in case an MCP client gave up before the job ended
    if lock:
        lock.close()
    return out

def init(repo):
    os.makedirs(os.path.join(repo, ".worker"), exist_ok=True)
    for name, starter in STARTERS.items():
        if not os.path.exists(os.path.join(repo, ".worker", name)):
            write_text(os.path.join(repo, ".worker", name), json.dumps(starter, indent=2) + "\n")
    exclude = os.path.join(repo, git(repo, "rev-parse", "--git-path", "info/exclude").stdout.decode().strip())
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a+", encoding="utf-8") as f:
        f.seek(0)
        if ".worker/" not in f.read().splitlines():
            f.write("\n.worker/wt/\n.worker/\n")  # the fixed worktree, and its neighbours: config, gates, job files
    print(f"worker: {repo}/.worker/config.json and gates.json are in place; .worker/ is excluded from git")

def serve_mcp(repo):
    """Stdio JSON-RPC by hand. Jobs run on threads, so a second call gets refused: busy and a cancel can arrive."""
    write_lock, running, fixed = threading.Lock(), {}, {"ping": {}, "tools/list": {"tools": [TOOL]}}

    def send(**msg):
        with write_lock:
            print(json.dumps(dict(msg, jsonrpc="2.0")), flush=True)

    def call(rid, params):
        args, token = params.get("arguments") or {}, (params.get("_meta") or {}).get("progressToken")
        beat = lambda line: token is not None and send(method="notifications/progress", params={
            "progressToken": token, "progress": time.time(), "message": line})
        out = run_job(repo, *map(args.get, ("task", "files", "mode", "edit_paths", "gate", "model")), beat, running[rid])
        if not running.pop(rid).is_set():  # a cancelled request gets no response
            send(id=rid, result={"content": [{"type": "text", "text": render(out)}], "isError": False})

    try:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
                method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
            except (ValueError, AttributeError):
                send(id=None, error={"code": -32700, "message": "parse error"})
                continue
            if method == "initialize":
                send(id=rid, result={"protocolVersion": params.get("protocolVersion", "2025-11-25"), "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "local-worker", "version": "1"}})
            elif method in fixed:
                send(id=rid, result=fixed[method])
            elif method == "tools/call" and params.get("name") == TOOL["name"]:
                running[rid] = threading.Event()
                threading.Thread(target=call, args=(rid, params)).start()
            elif method == "notifications/cancelled":
                running.get(params.get("requestId"), threading.Event()).set()
            elif rid is not None:
                send(id=rid, error={"code": -32601, "message": "method or tool not supported"})
    finally:  # the client went away or killed us: stop the job and its gate
        for cancel in list(running.values()):
            cancel.set()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="any path in the repository (default: the current directory)")
    ap.add_argument("--init", action="store_true", help="write starter .worker/ files and the git exclude entry")
    ap.add_argument("--mcp", action="store_true", help="serve stdio MCP")
    ap.add_argument("--task-file")
    ap.add_argument("--files", default="", help="comma-separated repo-relative paths")
    ap.add_argument("--mode", choices=("read", "edit"))
    ap.add_argument("--edit-paths", default="", help="comma-separated subset of --files; a file not yet existing may be named")
    ap.add_argument("--gate", help="name from .worker/gates.json; required for edit")
    ap.add_argument("--model", choices=("qwen36", "qwen38"), default="qwen36", help="qwen36 is the faster one")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))  # so the finally blocks above stop a running gate
    top = subprocess.run(["git", "-C", args.repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode:
        ap.error(f"not a git repository: {args.repo} (use --repo, or run from inside the repository)")
    repo = os.path.realpath(top.stdout.rstrip("\n"))
    if args.init or args.mcp:
        return init(repo) if args.init else serve_mcp(repo)
    if not args.task_file or not args.mode:
        ap.error("--task-file and --mode are required")
    try:
        task = read_text(args.task_file)
    except (OSError, UnicodeError) as e:
        ap.error(f"cannot read --task-file: {e}")
    out = run_job(repo, task, [p for p in args.files.split(",") if p], args.mode, [p for p in args.edit_paths.split(",") if p],
                  args.gate, args.model, lambda line: print(line, file=sys.stderr, flush=True))
    print(render(out))
    sys.exit(EXIT[out["status"].split(":")[0]])

if __name__ == "__main__":
    main()
