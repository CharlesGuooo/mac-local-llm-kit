#!/usr/bin/env python3
"""Generate the R3 log fixture and its answer key. Deterministic: same seed, same bytes.
Usage: gen_log.py out.log key.json"""
import json, random, sys
from datetime import datetime, timedelta

if len(sys.argv) != 3:
    sys.exit(__doc__)

rng = random.Random(20261107)
t = datetime(2026, 11, 7, 9, 0, 0)
ep = ["/api/orders", "/api/users", "/api/cart", "/api/search", "/health"]
lines = []
key = {"counts": {"E_DB_TIMEOUT": 0, "E_UPSTREAM_502": 0, "E_AUTH_EXPIRED": 0, "E_DISK_FULL": 0},
       "first_seen": {}, "retryable": {}}
retry = {"E_DB_TIMEOUT": True, "E_UPSTREAM_502": True, "E_AUTH_EXPIRED": False, "E_DISK_FULL": False}
slow = {e: [] for e in ep}

def err(code, msg):
    key["counts"][code] += 1
    key["first_seen"].setdefault(code, t.strftime("%H:%M:%S"))
    key["retryable"][code] = retry[code]
    return f"{t:%H:%M:%S} worker-{rng.randint(1, 4)} code={code} {msg}"

for i in range(500):
    t += timedelta(seconds=rng.randint(1, 9))
    e = rng.choice(ep)
    ms = rng.randint(5, 180)
    if 120 <= i < 150 and rng.random() < 0.5:
        lines.append(err("E_DB_TIMEOUT", f"query on {e} exceeded 30s pool=main"))
        continue
    if 300 <= i < 320 and rng.random() < 0.6:
        lines.append(err("E_UPSTREAM_502", f"gateway returned 502 for {e} retry_after=2"))
        continue
    if i in (77, 201, 402):
        lines.append(err("E_AUTH_EXPIRED", f"token expired for {e} user=u{rng.randint(100, 999)}"))
        continue
    if i == 455:
        lines.append(err("E_DISK_FULL", "write failed path=/var/log/app/audit.log free=0B"))
        continue
    if e == "/api/search" and 200 <= i < 260:
        ms = rng.randint(900, 1600)
    slow[e].append(ms)
    lines.append(f"{t:%H:%M:%S} worker-{rng.randint(1, 4)} GET {e} 200 {ms}ms")

key["slowest_endpoint_by_mean"] = max(ep, key=lambda x: sum(slow[x]) / len(slow[x]))
key["total_lines"] = len(lines)
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
json.dump(key, open(sys.argv[2], "w"), indent=1, sort_keys=True)
