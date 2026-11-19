#!/usr/bin/env python3
"""Calibration gates. Run from the itsdangerous worktree root. Exit 0 means pass; prints one JSON line."""
import ast, collections, json, os, re, subprocess, sys

BASE = "672971d66a2ef9f85151e53283113f33d642dabd"
PKG = "src/itsdangerous/"
# Model-written tests run under this, so tokens in the shell stay out of reach. It is no sandbox: they can still read files.
ENV = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "TERM", "MYPY_CACHE_DIR")
       if k in os.environ}
ENV["PYTHONPATH"] = "src"
R1_KEY = {104: "BadSignature", 106: "BadTimeSignature", 126: "BadTimeSignature", 130: "BadTimeSignature",
          135: "BadTimeSignature", 142: "SignatureExpired", 149: "SignatureExpired"}
ALL_KEY = {"encoding": {"base64_decode", "base64_encode", "bytes_to_int", "int_to_bytes", "want_bytes"},
           "exc": {"BadData", "BadHeader", "BadPayload", "BadSignature", "BadTimeSignature", "SignatureExpired"},
           "signer": {"HMACAlgorithm", "NoneAlgorithm", "Signer", "SigningAlgorithm"},
           "timed": {"TimedSerializer", "TimestampSigner"},
           "serializer": {"Serializer", "is_text_serializer"},
           "url_safe": {"URLSafeSerializer", "URLSafeSerializerMixin", "URLSafeTimedSerializer"}}
MUTANTS = [('.lstrip(b"\\x00")', '.strip(b"\\x00")'),
           ('bytestr.rjust(8, b"\\x00")', 'bytestr.ljust(8, b"\\x00")'),
           ('.rstrip(b"=")', ''),
           ('base64.urlsafe_b64encode(string)', 'base64.b64encode(string)'),
           ('errors="ignore"', 'errors="strict"'),
           ('raise BadData("Invalid base64-encoded data") from e', 'return b""'),
           ('s.encode(encoding, errors)', 's.encode("utf-8", errors)')]
ENC = PKG + "encoding.py"
# Files and caches the harness itself leaves in the worktree; never part of a solution.
HARNESS = {".cal_baseline.json", "result.json", "answer.json", "app.log", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
L3_TEST = '''from itsdangerous.encoding import bytes_to_int
from itsdangerous.encoding import int_to_bytes


def test_int_bytes_trailing_zero():
    assert int_to_bytes(256) == b"\\x01\\x00"
    assert bytes_to_int(int_to_bytes(256)) == 256
'''
HIDDEN = {"l3": '''from itsdangerous.encoding import bytes_to_int, int_to_bytes
def test_hidden():
    for v in (1, 255, 256, 65280, 65536, 2**32, 2**63):
        assert int_to_bytes(v) == v.to_bytes(8, "big").lstrip(b"\\x00")
        assert bytes_to_int(int_to_bytes(v)) == v
''', "l4": '''from itsdangerous.signer import Signer
from itsdangerous.timed import TimestampSigner
def test_hidden():
    assert Signer("k").unsign(Signer("k").sign(b"a.b.c")) == b"a.b.c"
    assert TimestampSigner("k").unsign(TimestampSigner("k").sign("v.w")) == b"v.w"
'''}


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, env=ENV)


def done(ok, **info):
    print(json.dumps({"pass": bool(ok), **info}, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def changed(rev):
    names = set(sh("git", "diff", "--name-only", rev).stdout.split()) | set(
        sh("git", "ls-files", "-o", "--exclude-standard").stdout.split())
    return {n for n in names if n.split("/")[0] not in HARNESS}


def show(path):
    return sh("git", "show", f"{BASE}:{path}").stdout


def replace_once(path, old, new):
    s = open(path, encoding="utf-8").read()
    if s.count(old) != 1:
        sys.exit(f"anchor not unique in {path}: {old}")
    open(path, "w", encoding="utf-8").write(s.replace(old, new))


def lint_counts():
    """mypy errors and ruff findings. A tool that did not run raises instead of counting as zero."""
    m = sh(sys.executable, "-m", "mypy")
    errors = m.stdout.count(": error:")
    if m.returncode and not errors:
        raise RuntimeError("mypy did not run: " + (m.stderr or m.stdout)[-200:])
    r = sh(sys.executable, "-m", "ruff", "check", "--no-fix", "--output-format=json", "src", "tests")
    if r.returncode not in (0, 1):
        raise RuntimeError("ruff did not run: " + r.stderr[-200:])
    return errors, len(json.loads(r.stdout))


def stock():
    """pytest green; mypy and ruff finding counts not above the saved baseline."""
    b = json.load(open(".cal_baseline.json"))
    t = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider").returncode
    m, r = lint_counts()
    return {"pytest_rc": t, "mypy": m, "mypy_base": b["mypy"], "ruff": r, "ruff_base": b["ruff"],
            "ok": t == 0 and m <= b["mypy"] and r <= b["ruff"]}


def strip_doc(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            n.body = n.body[1:] or [ast.Pass()]
    return ast.dump(tree)


def uses_skip_or_mock(tree):
    """Look at names in code, so a comment saying "no mocks" does not count."""
    for n in ast.walk(tree):
        names = [getattr(n, "id", None), getattr(n, "attr", None), getattr(n, "module", None)]
        names += [a.name for a in getattr(n, "names", []) if isinstance(a, ast.alias)]
        for name in filter(None, names):
            if "mock" in name.lower() or name in ("skip", "skipif", "xfail", "importorskip"):
                return True
    return False


def assert_count(path):
    bad = []
    tree = ast.parse(open(path, encoding="utf-8").read())
    tests = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
    for f in tests:
        src = ast.unparse(f)
        if not (any(isinstance(x, ast.Assert) for x in ast.walk(f)) or "pytest.raises" in src):
            bad.append(f.name)
    return len(tests), bad, uses_skip_or_mock(tree)


def main(cmd, *args):
    if cmd == "baseline":
        m, r = lint_counts()
        t = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
        json.dump({"mypy": m, "ruff": r}, open(".cal_baseline.json", "w"))
        done(t.returncode == 0, mypy=m, ruff=r, pytest=t.stdout.strip().splitlines()[-1:])
    if cmd == "setup":
        job = args[0]
        if job not in ("l2", "l3", "l4"):
            sys.exit(f"unknown setup job {job}")
        if job == "l2":
            replace_once(PKG + "timed.py", "if age > max_age:", "if age >= max_age:")
        elif job == "l3":
            replace_once(ENC, '.lstrip(b"\\x00")', '.strip(b"\\x00")')
            open("tests/test_itsdangerous/test_int_zero.py", "w").write(L3_TEST)
        elif job == "l4":
            replace_once(PKG + "signer.py", "signed_value.rsplit(self.sep, 1)", "signed_value.split(self.sep, 1)")
        sh("git", "add", "-A", "src", "tests")
        c = sh("git", "-c", "user.name=cal", "-c", "user.email=cal@local", "-c", "commit.gpgsign=false",
               "commit", "--no-verify", "-qm", f"cal-setup {job}")
        done(c.returncode == 0, setup=job, head=sh("git", "rev-parse", "HEAD").stdout.strip(), error=c.stderr[-200:])
    if cmd == "r1":
        a = json.load(open(args[0]))
        got = {int(x["line"]): x["exception"] for x in a}
        miss = [k for k in R1_KEY if got.get(k) != R1_KEY[k]]
        extra = [k for k in got if k not in R1_KEY]
        done(not miss and not extra, omitted=miss, fabricated=extra)
    if cmd == "r2":
        want = set()
        for root, _, fs in os.walk("src"):
            for f in fs:
                if f.endswith(".py"):
                    p = os.path.join(root, f)
                    for n in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
                        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "want_bytes":
                            want.add(f"{p}:{n.lineno}")
        got = set(json.load(open(args[0])))
        done(got == want, want=len(want), omitted=sorted(want - got), fabricated=sorted(got - want))
    if cmd == "r3":
        a, k = json.load(open(args[0])), json.load(open(args[1]))
        bad = [f for f in ("counts", "first_seen", "retryable", "slowest_endpoint_by_mean") if a.get(f) != k[f]]
        done(not bad, wrong_fields=bad)
    if cmd == "m1":
        allow = {PKG + x for x in ("__init__.py", "encoding.py", "serializer.py", "signer.py", "timed.py")} | {
            "tests/test_itsdangerous/test_encoding.py"}
        ch = changed(BASE)
        left = sh("git", "grep", "-nw", "want_bytes", "--", "src", "tests").stdout.strip()
        bad = []
        for p in ch & allow:
            rev = re.sub(r"\bensure_bytes\b", "want_bytes", open(p, encoding="utf-8").read()).splitlines()
            if collections.Counter(rev) != collections.Counter(show(p).splitlines()):
                bad.append(p)
        s = stock()
        done(not left and ch == allow and not bad and s["ok"], left=left[:200], scope=sorted(ch - allow),
             missed=sorted(allow - ch), other_edits=bad, stock=s)
    if cmd == "m2":
        ch = changed(BASE)
        new = ast.parse(open(ENC, encoding="utf-8").read())
        same = strip_doc(ast.parse(open(ENC, encoding="utf-8").read())) == strip_doc(ast.parse(show(ENC)))
        missing = [n.name for n in new.body if isinstance(n, ast.FunctionDef) and not ast.get_docstring(n)]
        keep = {n.name: ast.get_docstring(n) for n in ast.parse(show(ENC)).body
                if isinstance(n, ast.FunctionDef) and ast.get_docstring(n)}
        altered = [n.name for n in new.body if isinstance(n, ast.FunctionDef) and n.name in keep
                   and ast.get_docstring(n) != keep[n.name]]
        s = stock()
        done(ch == {ENC} and same and not missing and not altered and s["ok"], scope=sorted(ch), code_changed=not same,
             missing=missing, altered=altered, stock=s)
    if cmd == "m3":
        ch = changed(BASE)
        allow = {PKG + m + ".py" for m in ALL_KEY}
        bad = {}
        for m, want in ALL_KEY.items():
            tree = ast.parse(open(PKG + m + ".py", encoding="utf-8").read())
            got = None
            rest = []
            for n in tree.body:
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "__all__":
                    got = set(ast.literal_eval(n.value))
                else:
                    rest.append(n)
            tree.body = rest
            if got != want or strip_doc(tree) != strip_doc(ast.parse(show(PKG + m + ".py"))):
                bad[m] = {"got": sorted(got or []), "want": sorted(want)}
        s = stock()
        done(ch == allow and not bad and s["ok"], scope=sorted(ch - allow), missed=sorted(allow - ch), wrong=bad, stock=s)
    if cmd == "l1":
        path = "tests/test_itsdangerous/test_encoding_more.py"
        ch = changed(BASE)
        n, hollow, cheat = assert_count(path)
        clean = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path).returncode
        killed = []
        orig = open(ENC, encoding="utf-8").read()
        for i, (old, new) in enumerate(MUTANTS):
            open(ENC, "w", encoding="utf-8").write(orig.replace(old, new))
            if sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path).returncode != 0:
                killed.append(i)
        open(ENC, "w", encoding="utf-8").write(orig)
        done(ch == {path} and clean == 0 and n >= 5 and not hollow and not cheat and len(killed) >= 5,
             scope=sorted(ch), clean_rc=clean, tests=n, hollow=hollow, skip_or_mock=cheat, killed=killed, of=len(MUTANTS))
    if cmd == "l2":
        path = "tests/test_itsdangerous/test_max_age_spec.py"
        ch = changed("HEAD")
        n, hollow, cheat = assert_count(path)
        buggy = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path).returncode
        t = PKG + "timed.py"
        orig = open(t, encoding="utf-8").read()
        open(t, "w", encoding="utf-8").write(orig.replace("if age >= max_age:", "if age > max_age:"))
        fixed = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path).returncode
        open(t, "w", encoding="utf-8").write(orig)
        done(ch == {path} and buggy != 0 and fixed == 0 and not hollow and not cheat, scope=sorted(ch),
             bug_not_caught=buggy == 0, asserts_current_bug=buggy == 0 and fixed != 0,
             fails_on_fixed_code=fixed != 0, hollow=hollow, skip_or_mock=cheat)
    if cmd in ("l3", "l4"):
        target = ENC if cmd == "l3" else PKG + "signer.py"
        ch = changed("HEAD")
        tests_touched = sh("git", "diff", "--name-only", "HEAD", "--", "tests").stdout.strip()
        stat = sh("git", "diff", "--numstat", "HEAD", "--", target).stdout.split()
        size = int(stat[0]) + int(stat[1]) if len(stat) >= 2 else 0
        s = stock()
        h = "tests/test_itsdangerous/test_hidden_cal.py"
        open(h, "w").write(HIDDEN[cmd])
        hid = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", h).returncode
        os.remove(h)
        done(ch == {target} and not tests_touched and size <= 6 and s["ok"] and hid == 0, scope=sorted(ch),
             tests_touched=tests_touched, diff_lines=size, stock=s, hidden_rc=hid)
    sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: gate.py baseline | setup l2|l3|l4 | r1|r2 answer.json | r3 answer.json key.json | m1|m2|m3|l1|l2|l3|l4")
    try:
        main(*sys.argv[1:])
    except Exception as e:  # a missing file or a malformed answer is a failed gate, still one JSON line
        done(False, error=f"{type(e).__name__}: {e}"[:300])
