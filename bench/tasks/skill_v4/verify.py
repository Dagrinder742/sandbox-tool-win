#!/usr/bin/env python3
"""Acceptance check for the v4 skill.py contract.

Usage:  python verify_skill.py [path/to/skill.py]      (default: ./skill.py)

It judges the FILE, not what the model said about it. It runs the file the same way
`python skill.py` would, with no sandbox guard, so only point it at code you would run
yourself. Last line is always:  VERIFY: PASS (n/n)  or  VERIFY: FAIL (k/n)
"""
import ast
import contextlib
import importlib.util
import io
import os
import signal
import subprocess
import sys

PATH = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "skill.py")
results = []
script_out = ""


class Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout("took longer than 10 seconds")


if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _alarm)


def check(name, fn):
    try:
        if hasattr(signal, "alarm"):
            signal.alarm(10)
        detail = fn()
        results.append((True, name, detail or ""))
    except Exception as e:
        results.append((False, name, f"{type(e).__name__}: {e}"[:220]))
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)


def load():
    spec = importlib.util.spec_from_file_location("skill_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


def t_import():
    load()


def t_runs():
    global script_out
    r = subprocess.run([sys.executable, PATH], capture_output=True, text=True, timeout=10,
                       stdin=subprocess.DEVNULL, cwd=os.path.dirname(PATH))
    script_out = r.stdout
    assert r.returncode == 0, f"exit {r.returncode}: " + (r.stderr.strip().splitlines() or [""])[-1]


def t_stages():
    for word in ("Perceive:", "Reason:", "Evaluate:", "Act:"):
        n = sum(1 for line in script_out.splitlines() if line.strip().startswith(word))
        assert n >= 3, f"'{word}' printed {n} times by `python skill.py`, expected 3"


def t_no_stubs():
    src = open(PATH).read()
    assert "TODO" not in src and "placeholder" not in src.lower(), "TODO/placeholder text found"
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [b for b in n.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                              and isinstance(b.value.value, str))]
            if not body or all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                                          and b.value.value is Ellipsis) for b in body):
                raise AssertionError(f"{n.name}() is an empty stub")


def t_state():
    s = load().Skill()
    assert isinstance(s.state, dict), "state is not a dict"
    assert "metrics" in s.state and "history" in s.state, f"state keys are {sorted(s.state)}"


def t_perceive():
    for _ in range(20):
        s = load().Skill()
        m, out = quiet(s.perceive)
        assert isinstance(m, dict), f"returned {type(m).__name__}, expected dict"
        for key, lo, hi in (("temperature", 10, 30), ("humidity", 40, 80), ("light", 0, 100)):
            assert key in m and isinstance(m[key], int) and lo <= m[key] <= hi, f"{key}={m.get(key)!r} not an int in {lo}-{hi}"
        assert s.state["metrics"] == m, "state['metrics'] was not updated"
        assert "Perceive:" in out, "did not print 'Perceive:'"


def t_reason():
    Skill = load().Skill
    for i in range(150):
        s = Skill()
        quiet(s.perceive)
        acts, out = quiet(s.reason)
        assert isinstance(acts, list), f"returned {type(acts).__name__}, expected list"
        assert len(acts) == 3, f"returned {len(acts)} actions on try {i + 1}: {acts!r}"
        assert all(isinstance(a, str) and a for a in acts), f"actions must be non-empty strings: {acts!r}"
        assert len(set(acts)) == 3, f"actions are not all different: {acts!r}"
    assert "Reason:" in out, "did not print 'Reason:'"


def t_evaluate():
    s = load().Skill()
    best, out = quiet(s.evaluate, ["a", "bbb", "cc"])
    assert best == "bbb", f"evaluate(['a','bbb','cc']) returned {best!r}, expected 'bbb'"
    tie, _ = quiet(s.evaluate, ["aa", "bb", "c"])
    assert tie == "aa", f"on a tie it returned {tie!r}, expected the first one, 'aa'"
    assert "Evaluate:" in out, "did not print 'Evaluate:'"


def t_act():
    s = load().Skill()
    r, out = quiet(s.act, "zzz")
    assert r == "zzz", f"returned {r!r}, expected the action"
    assert "Act:" in out and "zzz" in out, "did not print 'Act: zzz'"


def t_think():
    Skill = load().Skill
    s = Skill()
    h, _ = quiet(s.think)
    assert isinstance(h, list) and len(h) == 3 and all(isinstance(x, str) for x in h), f"think() returned {h!r}"
    assert s.state["history"] == h, "state['history'] does not match the returned list"
    h5, _ = quiet(Skill().think, rounds=5)
    assert len(h5) == 5, f"think(rounds=5) returned {len(h5)} items"


if not os.path.isfile(PATH):
    results.append((False, "skill.py exists", f"not found: {PATH}"))
else:
    check("file imports without error", t_import)
    importable = results[-1][0]
    for name, fn in (("script runs and exits 0", t_runs), ("script prints each stage 3 times", t_stages),
                     ("no stub bodies or TODO", t_no_stubs), ("state has metrics + history", t_state),
                     ("perceive() contract", t_perceive), ("reason() always returns 3 different actions", t_reason),
                     ("evaluate() picks the longest, first on ties", t_evaluate), ("act() prints and returns", t_act),
                     ("think() loops, records history", t_think)):
        if name.startswith(("state", "perceive", "reason", "evaluate", "act", "think")) and not importable:
            results.append((False, name, "skipped: the file does not import"))
        else:
            check(name, fn)

for ok, name, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not ok else ""))
good = sum(1 for r in results if r[0])
print(f"VERIFY: {'PASS' if good == len(results) else 'FAIL'} ({good}/{len(results)})")
sys.exit(0 if good == len(results) else 1)
