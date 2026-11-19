#!/usr/bin/env python3
# 用法见下文。只用标准库。llama.cpp、oMLX 的 OpenAI 接口都能用。
import argparse, json, os, statistics as st, time, urllib.request as u, uuid

TXT = ("Unified memory bandwidth limits how fast a dense model can read its weights for each token. "
       "Prompt caches keep attention state so that later turns only process new tokens. ") 

def doc(tokens):                 # 约 4.42 字符/token,只用来凑长度,真实值看服务器返回的 prompt_tokens
    return uuid.uuid4().hex + " " + (TXT * (int(tokens * 4.42 / len(TXT)) + 1))

def call(a, messages, max_tokens):
    body = {"messages": messages, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True}}
    if a.model: body["model"] = a.model
    if a.effort: body["reasoning_effort"] = a.effort
    headers = {"Content-Type": "application/json"}
    if a.api_key: headers["Authorization"] = "Bearer " + a.api_key
    req = u.Request(a.url + "/v1/chat/completions", json.dumps(body).encode(), headers)
    t0 = time.time(); t1 = t2 = None; text = rsn = ""; usage = {}; tim = {}; finish = None; n = 0
    with u.urlopen(req, timeout=36000) as r:
        for line in r:
            if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"): continue
            j = json.loads(line[6:])
            usage = j.get("usage") or usage; tim = j.get("timings") or tim
            for c in j.get("choices", []):
                d = c.get("delta", {}); finish = c.get("finish_reason") or finish
                piece = d.get("content") or ""; think = d.get("reasoning_content") or d.get("reasoning") or ""
                if piece or think:
                    t1 = t1 or time.time(); t2 = time.time(); n += 1; text += piece; rsn += think
    ptok = usage.get("prompt_tokens", 0); ctok = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    gen = usage.get("completion_tokens", n)
    if "cache_n" in tim: ctok = tim["cache_n"]
    reproc = tim.get("prompt_n", ptok - (ctok or 0))
    res = dict(prompt_tokens=ptok, reprocessed=reproc, gen=gen, finish=finish,
               ttft_s=round((t1 or time.time()) - t0, 2),
               prefill=round(reproc / max((t1 or t0 + 1e-3) - t0, 1e-3), 1),
               decode=round(gen / max((t2 or t0) - (t1 or t0), 1e-3), 1))
    return res, text, rsn

def once(a):
    q = doc(a.prompt_tokens) + "\n\n" + a.ask if a.prompt_tokens else a.ask   # 0 = 不加填充文本
    res, _, _ = call(a, [{"role": "user", "content": q}], a.max_tokens)
    res["A_low_min"] = round((1000 / max(res["prefill"], 1e-3) + 1895 / max(res["decode"], 1e-3)) / 60, 2)
    print(json.dumps(res))
    return res

def soak(a):
    start = time.time(); rows = []
    while time.time() - start < a.minutes * 60:
        r = once(a); r["t"] = time.time() - start; rows.append(r)
    w = min(5 * 60, a.minutes * 60 / 4)
    first = [r["decode"] for r in rows if r["t"] <= w] or [rows[0]["decode"]]
    last = [r["decode"] for r in rows if r["t"] >= a.minutes * 60 - w] or [rows[-1]["decode"]]
    print("first5min", st.mean(first), "last5min", st.mean(last), "ratio", round(st.mean(last) / st.mean(first), 3))

def turns(a):
    msgs = [{"role": "user", "content": doc(a.prompt_tokens) + "\n\nRead this. Reply with one short sentence."}]
    rows = []
    for i in range(a.turns):
        if a.edit_turn == i: msgs[0]["content"] = "x" + msgs[0]["content"]   # 模拟改动很早的历史
        r, text, rsn = call(a, msgs, a.max_tokens)
        print(json.dumps(dict(turn=i, **r))); rows.append(r["reprocessed"])
        m = {"role": "assistant", "content": text}
        if rsn and not a.drop_reasoning: m["reasoning_content"] = rsn         # 回传思考内容才能保住前缀
        msgs += [m, {"role": "user", "content": f"Turn {i}: add one more short sentence."}]
    print("median reprocessed after turn 0:", st.median(rows[1:]) if len(rows) > 1 else None)

p = argparse.ArgumentParser()
p.add_argument("mode", choices=["once", "soak", "turns"])
p.add_argument("--url", default="http://127.0.0.1:8080"); p.add_argument("--model"); p.add_argument("--effort")
p.add_argument("--api-key", default=os.environ.get("OMLX_API_KEY"))
p.add_argument("--prompt-tokens", type=int, default=1000); p.add_argument("--max-tokens", type=int, default=1895)
p.add_argument("--ask", default="Explain, in detail and in prose, how memory bandwidth and caching shape local inference.")
p.add_argument("--minutes", type=float, default=20); p.add_argument("--turns", type=int, default=10)
p.add_argument("--edit-turn", type=int, default=-1); p.add_argument("--drop-reasoning", action="store_true")
a = p.parse_args()
{"once": once, "soak": soak, "turns": turns}[a.mode](a)
