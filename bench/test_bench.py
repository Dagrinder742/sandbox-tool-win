#!/usr/bin/env python3
"""Checks for bench.py itself, using a fake model server (no real model needed).
Run:  python test_bench.py      -> must end with ALL OK
It copies the kit to a temp folder first, so your real runs/ and reports/ are never touched."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path(os.environ.get("TOOLS") or HERE.parent / "src")
if not (TOOLS / "sandbox_tool.py").is_file():
    sys.exit(f"STOP: sandbox_tool.py not found (set TOOLS=<folder>), looked in {TOOLS}")

bad = 0


def check(ok, what, detail=""):
    global bad
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  -> {detail}"[:200] if (detail and not ok) else ""))


def sel(name):
    return (HERE / "tasks" / name / "selftest" / "good.py").read_text()


# ---- fake llama-server: scripted replies, chosen by the task text in the first user message --------
BAD_STACK = (HERE / "tasks" / "stack" / "selftest" / "bad_no_error.py").read_text()
PO_CLEAN = ("def clean(self):\n    good = []\n    for row in self.rows:\n        if row.get(\"item\") and row.get(\"qty\", 0) > 0:\n"
            "            good.append({\"item\": row[\"item\"], \"price\": float(row[\"price\"]), \"qty\": row[\"qty\"]})\n    return good")
PO_AVG = "def average_order(rows):\n    if not rows:\n        return 0.0\n    return subtotal(rows) / len(rows)"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"data": [{"id": "stub-model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = req["messages"]
        first = next(m["content"] for m in msgs if m["role"] == "user")
        step = sum(m["role"] == "assistant" for m in msgs)
        P = lambda fn, code: [f"NOTE: write it\nCMD: write_file {fn}\n```python\n{code}```",
                              f"NOTE: run it\nCMD: run_python {fn}", "NOTE: finished\nDONE: it works"]
        if "Your first reply must be only" in first:   # pipeline_omni_v2: says DONE too early on orders=4 (zip24-omni)
            RD = lambda f, n, code: f"CMD: replace_def {f} {n}\n```python\n{code}\n```"
            plan = ["CMD: run_python main.py",
                    RD("ingest.py", "DataIngester.clean", PO_CLEAN.replace("> 0", ">= 0")) + "\n"
                    + RD("calc.py", "average_order", PO_AVG) + "\nCMD: run_python main.py",
                    "DONE: it works",
                    "NOTE: the gate says orders=4, the mug has qty 0\n" + RD("ingest.py", "DataIngester.clean", PO_CLEAN)
                    + "\nCMD: run_python main.py",
                    "DONE: prints the two expected lines"]
        elif "ingest.py" in first:                  # pipeline_omni: fix ingest.py and calc.py, main.py untouched
            RD = lambda f, n, code: f"CMD: replace_def {f} {n}\n```python\n{code}\n```"
            plan = ["NOTE: run it\nCMD: run_python main.py",
                    "NOTE: price is a string from ingest.py\n" + RD("ingest.py", "DataIngester.clean", PO_CLEAN) + "\n"
                    + RD("calc.py", "average_order", PO_AVG) + "\nCMD: run_python main.py",
                    "NOTE: finished\nDONE: prints the two lines"]
        elif "wc.py" in first:
            plan = P("wc.py", sel("wordcount"))
        elif "stack.py" in first:
            plan = P("stack.py", BAD_STACK)          # will FAIL verification but the model will say DONE
        elif "total.py" in first:
            plan = P("total.py", sel("csv_total"))
        elif "average.py" in first:
            plan = ["NOTE: look\nCMD: read_file average.py",
                    "NOTE: fix empty\nCMD: patch_file average.py\n<<<<<<< SEARCH\ndef average(numbers):\n    total = 0\n=======\n"
                    "def average(numbers):\n    if not numbers:\n        return 0.0\n    total = 0\n>>>>>>> REPLACE",
                    "NOTE: fix median\nCMD: patch_file average.py\n<<<<<<< SEARCH\n    s = numbers\n    s.sort()\n    mid = len(s) // 2\n"
                    "    if len(s) % 2 == 0:\n        return s[mid]\n=======\n    s = sorted(numbers)\n    mid = len(s) // 2\n"
                    "    if len(s) % 2 == 0:\n        return (s[mid - 1] + s[mid]) / 2\n>>>>>>> REPLACE",
                    "NOTE: run\nCMD: run_python average.py", "NOTE: finished\nDONE: fixed"]
        else:
            plan = P("nothing.py", "print('x')\n")
        reply = plan[min(step, len(plan) - 1)]
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}],
                           "timings": {"prompt_n": 10, "prompt_per_second": 5.0, "predicted_n": 20,
                                       "predicted_per_second": 2.5}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"

tmp = Path(tempfile.mkdtemp(prefix="bench_test_"))
kit = tmp / "kit"
shutil.copytree(HERE, kit, ignore=shutil.ignore_patterns("runs", "work", "reports", "exports", "logs", "__pycache__", "misc"))
tools = tmp / "tools"
tools.mkdir()
shutil.copy(TOOLS / "sandbox_tool.py", tools)
env = dict(os.environ, LLAMA_URL=URL, BACKEND="local", LLAMA_STREAM="0", TOOLS=str(tools))
env.pop("RUN_PYTHON", None)


def bench(*args, **kw):
    return subprocess.run([sys.executable, str(kit / "bench.py"), *args], capture_output=True, text=True, env=env,
                          timeout=300, **kw)


# ---- 1. verifiers ------------------------------------------------------------------------------------
r = bench("selftest")
check(r.returncode == 0 and "ALL VERIFIERS BEHAVE" in r.stdout, "selftest: every verifier passes its good file and fails its bad ones", r.stdout[-300:] + r.stderr[-200:])
_po_lines = [l for l in r.stdout.splitlines() if l.startswith("ok") and l.split()[1:2] == ["pipeline_omni"]]
_po2_lines = [l for l in r.stdout.splitlines() if l.startswith("ok") and l.split()[1:2] == ["pipeline_omni_v2"]]
check(len(_po_lines) == 10 and any("good/ " in l and "PASS (13/13)" in l for l in _po_lines)
      and any("bad_hidden_in_main/" in l and "FAIL" in l for l in _po_lines),
      "selftest judges the 10 multi-file selftest FOLDERS of pipeline_omni (good/ PASS 13/13, bad_hidden_in_main/ FAIL)",
      "\n".join(_po_lines))
r = bench("list")
check(all(t in r.stdout for t in ("wordcount", "stack", "bugfix", "csv_total", "skill_v4", "skill_v42", "pipeline_omni", "pipeline_omni_v2")),
      "list shows all 8 tasks")
check("pipeline_omni_v2" in r.stdout, "list shows pipeline_omni_v2")

# ---- 2. a full run against the fake server ------------------------------------------------------------
r = bench("run", "-t", "wordcount", "stack", "-n", "2", "--label", "Stub Test", "--note", "first note")
check(r.returncode == 0, "run exits 0", r.stdout[-400:] + r.stderr[-400:])
sess = sorted((kit / "runs").glob("*stub-test*"))[0]
check(sess is not None and "stub-test" in sess.name, "session folder is named from the label", str(sess))
meta = json.loads((sess / "meta.json").read_text())
check(meta["model"] == "stub-model" and meta["runs_per_task"] == 2 and len(meta["tool_sha"]) == 10 and len(meta["system_sha"]) == 10
      and set(meta["tasks"]) == {"wordcount", "stack"}, "meta records model, hashes, tasks", str(meta)[:200])
res = {(p.parts[-3], p.parts[-2]): json.loads(p.read_text()) for p in sess.glob("*/run_*/result.json")}
check(len(res) == 4, "4 runs captured", str(list(res)))
w1, s1 = res[("wordcount", "run_1")], res[("stack", "run_1")]
check(w1["verify"] == "PASS" and w1["passed"] == w1["total"] == 13 and w1["ended"] == "done" and w1["steps"] == 3, "wordcount run is verified PASS", str(w1))
check(w1["cmds"] == {"write_file": 1, "run_python": 1, "DONE": 1}, "commands counted from the transcript", str(w1["cmds"]))
check(w1["gen_tok_s"] is not None, "speed parsed from the [llm] lines", str(w1["gen_tok_s"]))
check(s1["verify"] == "FAIL" and s1["ended"] == "done" and s1["claimed_false"] is True, "false DONE is detected (model said DONE, verifier said FAIL)", str(s1))
check(any("raises IndexError" in c for c in s1["failed_checks"]), "failed check names are recorded", str(s1["failed_checks"]))
rd = sess / "wordcount" / "run_1"
check((rd / "wc.py").is_file() and (rd / "run.log").is_file() and (rd / "verify.log").is_file() and (rd / "transcript.json").is_file(),
      "run folder has the output file, run.log, verify.log, transcript.json")
tr = json.loads((rd / "transcript.json").read_text())
check(tr["messages"][0]["role"] == "system" and tr["messages"][1]["role"] == "user" and tr["result"] and not tr["stopped"],
      "transcript has system, user, replies and the result")
check(not list((kit / "work").glob("*")), "per-run sandbox folders are cleaned up")
rep = (kit / "reports" / f"{sess.name}.txt").read_text()
for what, needle in (("setup block", "SETUP"), ("summary row", "wordcount"), ("false-DONE flagged", "CLAIMED DONE BUT FAILED"),
                     ("your notes section", "first note"), ("run log embedded", "model> NOTE: write it"),
                     ("model's file embedded", "def count_words"), ("verify log embedded", "VERIFY: PASS (13/13)")):
    check(needle in rep, f"report contains: {what}")
# ---- multi-file task (update 21): pipeline_omni through bench run, edits to calc.py / ingest.py are kept and judged ----
check(len(_po2_lines) == 10, "selftest judges the 10 selftest folders of pipeline_omni_v2 too", "\n".join(_po2_lines))
r = bench("run", "-t", "pipeline_omni_v2", "-n", "1", "--label", "Omni2 Stub")
_s2 = sorted((kit / "runs").glob("*omni2-stub*"))
_r2 = json.loads(next(_s2[0].glob("pipeline_omni_v2/run_1/result.json")).read_text()) if _s2 else {}
check(r.returncode == 0 and _r2.get("verify") == "PASS" and _r2.get("passed") == 13, "pipeline_omni_v2 stub run is verified PASS 13/13", str(_r2)[:200])
check(_r2.get("expect_blocks") == 1 and _r2.get("ended") == "done", "update 25: the expected-output gate refused the early DONE on orders=4 once, then the fixed run ended DONE",
      str({k: _r2.get(k) for k in ("expect_blocks", "ended", "steps")}))
_rep2 = (kit / "reports" / f"{_s2[0].name}.txt").read_text() if _s2 else ""
check("EXPECTED-OUTPUT GATE ON (expected_output" in _rep2 and "refused a DONE on wrong output 1 time(s), in 1 of 1 run(s): runs 1" in _rep2
      and "expected-output gate refused DONE: 1" in _rep2, "the report says the gate was ON for v2 and counts its refusals (summary + run line)")
_log2 = (_s2[0] / "pipeline_omni_v2" / "run_1" / "run.log").read_text() if _s2 else ""
check("[expect] DONE needs a run that prints exactly 2 expected line(s)" in _log2, "the run log shows the gate was active")
_m2 = json.loads((_s2[0] / "meta.json").read_text()) if _s2 else {}
r = bench("run", "-t", "pipeline_omni", "-n", "1", "--label", "Omni Stub")
po_sess = sorted((kit / "runs").glob("*omni-stub*"))
po = json.loads(next(po_sess[0].glob("pipeline_omni/run_1/result.json")).read_text()) if po_sess else {}
check(r.returncode == 0 and po.get("verify") == "PASS" and po.get("passed") == po.get("total") == 13 and po.get("ended") == "done",
      "pipeline_omni stub run: the multi-file fix is verified PASS 13/13", str(po)[:200] + r.stderr[-200:])
po_rd = po_sess[0] / "pipeline_omni" / "run_1" if po_sess else tmp
_m1 = json.loads((po_sess[0] / "meta.json").read_text()) if po_sess else {}
_t1, _t2 = _m1.get("tasks", {}).get("pipeline_omni", {}), _m2.get("tasks", {}).get("pipeline_omni_v2", {})
_old_env = env.get("AGENT_EXPECT_FILE")
env["AGENT_EXPECT_FILE"] = str(tmp / "stale_expected_from_the_shell.txt")
r = bench("run", "-t", "csv_total", "-n", "1", "--label", "stale expect")
env.pop("AGENT_EXPECT_FILE")
_se = sorted((kit / "runs").glob("*stale-expect*"))
_sr = json.loads(next(_se[0].glob("csv_total/run_1/result.json")).read_text()) if _se else {}
check(_sr.get("verify") == "PASS" and "[expect]" not in (_se[0] / "csv_total" / "run_1" / "run.log").read_text(),
      "an AGENT_EXPECT_FILE left in the shell never switches the gate on for a task without expected_output.txt", str(_sr)[:150])
check(_t1.get("expect_sha") is None and _t2.get("expect_sha") and "[expect]" not in (po_rd / "run.log").read_text(),
      "pipeline_omni (v1 baseline) has NO expected-output gate; v2 has one", f"{_t1.get('expect_sha')} {_t2.get('expect_sha')}")
check(_t1 and _t2 and _t1["verify_sha"] == _t2["verify_sha"] and _t1["prompt_sha"] != _t2["prompt_sha"],
      "pipeline_omni and _v2 share the SAME verifier hash and differ only in the prompt", f"{_t1} {_t2}")
check(all((po_rd / n).is_file() for n in ("main.py", "calc.py", "ingest.py")) and "float(row" in (po_rd / "ingest.py").read_text()
      and not list(po_rd.glob("extra_*")), "pipeline_omni: the EDITED calc.py and ingest.py are saved in the run folder (not as extras)")
po_rep = (kit / "reports" / f"{po_sess[0].name}.txt").read_text() if po_sess else ""
check("----- calc.py (the file the model wrote)" in po_rep and "----- ingest.py (the file the model wrote)" in po_rep
      and "----- main.py (the file the model wrote)" in po_rep, "pipeline_omni report shows all three files")
import re as _re
check(_re.search(r"wordcount\s+2/2\s+0", rep) and _re.search(r"stack\s+0/2\s+2", rep), "report summary shows pass counts and false-DONE counts")

# ---- 2b. tasks that need fixture files: csv_total (reads sales.csv) and bugfix (patch_file workflow) --------
r = bench("run", "-t", "csv_total", "bugfix", "-n", "1", "--label", "fixtures")
fx = sorted((kit / "runs").glob("*fixtures*"))[-1]
for t in ("csv_total", "bugfix"):
    rr = json.loads((fx / t / "run_1" / "result.json").read_text())
    check(rr["verify"] == "PASS" and rr["ended"] == "done", f"{t}: fixtures were in the sandbox and the run verified PASS", str(rr)[:300] + (fx / t / "run_1" / "run.log").read_text()[-600:])
rr = json.loads((fx / "bugfix" / "run_1" / "result.json").read_text())
check(rr["cmds"].get("patch_file") == 2 and rr["cmds"].get("read_file") == 1, "bugfix run used read_file and patch_file as scripted", str(rr["cmds"]))

# ---- 3. notes, report rebuild, show, stats, export ---------------------------------------------------
# (the session is named explicitly: "latest" depends on which minute each session started in, which made this flaky)
r = bench("note", "model keeps forgetting the import", "--session", sess.name)
check(r.returncode == 0, "note (session)", r.stderr)
r = bench("note", "this one looked odd", "--run", "stack/1", "--session", sess.name)
check(r.returncode == 0, "note (one run)", r.stderr)
r = bench("note", "x", "--run", "stack/99", "--session", sess.name)
check(r.returncode != 0 and "no run" in r.stderr, "note on a missing run is refused")
r = bench("report", sess.name, "--detail", "fail")
rep2 = (kit / "reports" / f"{sess.name}.txt").read_text()
check("model keeps forgetting the import" in rep2 and "this one looked odd" in rep2, "rebuilt report picks up new notes")
check("def count_words" not in rep2 and "def pop" in rep2, "--detail fail leaves out passing runs' bodies and keeps failing ones")
r = bench("show", "wordcount/1", sess.name)
check("model> NOTE: write it" in r.stdout and "def count_words" in r.stdout and "VERIFY: PASS" in r.stdout, "show prints log + file + verify like your cat chain")
r = bench("stats")
check("stub-test" in r.stdout and "wordcount 2/2" in r.stdout and "stack 0/2" in r.stdout, "stats shows pass rates", r.stdout)
r = bench("export", "-o", str(tmp / "out.jsonl"))
rows = [json.loads(l) for l in (tmp / "out.jsonl").read_text().splitlines()]
check(sorted(r["task"] for r in rows) == ["bugfix", "csv_total", "pipeline_omni", "pipeline_omni_v2", "wordcount"] and rows[0]["messages"][-1]["role"] == "assistant",
      "export keeps verified passes only (2 identical wordcount runs de-duplicated to 1, stack FAILs excluded)", str([r["task"] for r in rows]))
check("WARNING" in r.stdout, "export prints the not-a-training-set warning")
r = bench("export", "-o", str(tmp / "out2.jsonl"), "--no-system")
rows2 = [json.loads(l) for l in (tmp / "out2.jsonl").read_text().splitlines()]
check(rows2 and rows2[0]["messages"][0]["role"] == "user", "--no-system drops the system prompt")

# ---- 4. honesty and safety -------------------------------------------------------------------------
r = bench("run", "-t", "nosuchtask")
check(r.returncode != 0 and "no task called" in r.stderr, "unknown task stops with a clear message")
shutil.copy(kit / "tasks" / "wordcount" / "prompt.txt", tmp / "keep_prompt.txt")
(kit / "tasks" / "wordcount" / "prompt.txt").write_text("")
before = len(list((kit / "runs").glob("*")))
r = bench("run", "-t", "wordcount", "-n", "1")
check(r.returncode != 0 and "Nothing was run" in r.stderr and len(list((kit / "runs").glob("*"))) == before,
      "an empty prompt stops BEFORE any run (no fake FAILs)", r.stderr)
shutil.copy(tmp / "keep_prompt.txt", kit / "tasks" / "wordcount" / "prompt.txt")
# server down: runs are recorded as failures with the reason, never as passes
env_dead = dict(env, LLAMA_URL="http://127.0.0.1:9/v1/chat/completions")
r = subprocess.run([sys.executable, str(kit / "bench.py"), "run", "-t", "wordcount", "-n", "1", "--label", "dead"],
                   capture_output=True, text=True, env=env_dead, timeout=120)
dead = sorted((kit / "runs").glob("*dead*"))[-1]
rr = json.loads(next(dead.glob("wordcount/run_1/result.json")).read_text())
check(rr["verify"] == "FAIL" and "stopped" in rr["ended"] and not rr["claimed_false"] and "no output file" in (dead / "wordcount/run_1/verify.log").read_text(),
      "server down: recorded as FAIL / stopped, not as a false DONE", str(rr)[:200])
# a model-written file with blocked constructs is NOT executed by the verifier
import importlib.util
spec = importlib.util.spec_from_file_location("bench_mod", kit / "bench.py")
bm = importlib.util.module_from_spec(spec); spec.loader.exec_module(bm)
evil = tmp / "evil.py"
marker = tmp / "pwned.txt"
evil.write_text(f"import os\nos.system('echo hi > ' + {str(marker)!r})\n")   # %r: a Windows path in '...' is a SyntaxError (W3)
tool = bm.load_tool(tools)
v, text, p, n = bm.judge(bm.all_tasks()["wordcount"], evil, tool)
check(v == "FAIL" and "did NOT run" in text and not marker.exists(), "verifier refuses to execute files with blocked constructs", text[:150])

# ---- W4: vkit's per-check timeout WITHOUT SIGALRM (Windows), forced here with VKIT_NO_ALARM=1 ----
import time as _time
_bf = bm.all_tasks()["bugfix"]
_hang = tmp / "average.py"
_hang.write_text("def average(numbers):\n    while True:\n        pass\n\n\ndef median(numbers):\n    s = sorted(numbers)\n"
                 "    m = len(s) // 2\n    return (s[m - 1] + s[m]) / 2 if len(s) % 2 == 0 else s[m]\n")
os.environ["VKIT_NO_ALARM"] = "1"
try:
    _t0 = _time.time()
    v, text, p, n = bm.judge(_bf, _hang, tool)
    _dt = _time.time() - _t0
    check(v == "FAIL" and text.count("Timeout: took longer than 10 seconds") >= 2 and "PASS  median, odd count" in text
          and text.strip().splitlines()[-1].startswith("VERIFY: FAIL") and _dt < 80, "W4 vkit without SIGALRM: a hanging check times out at 10 s, the others still run and the VERIFY line is printed",
          f"{_dt:.0f}s " + text[-200:])
    _hang.write_text("while True:\n    pass\n")      # hangs at IMPORT, inside load()'s stdout redirect
    v, text, p, n = bm.judge(_bf, _hang, tool)
    check(text.strip().splitlines()[-1].startswith("VERIFY: FAIL") and "Timeout" in text, "W4 vkit without SIGALRM: a file that hangs at import still gets its VERIFY line (stdout restored)", text[-160:])
finally:
    os.environ.pop("VKIT_NO_ALARM", None)

# ---- W4: on Windows the verifier runs inside a Job Object (tested here with a stand-in job) ----
class _FakeVJob:
    made, fail_assign = [], False

    def __init__(self, mem_mb, cpu_s, procs=1):
        self.args, self.proc, self.term, self.closed, self.waiting = (mem_mb, cpu_s, procs), None, None, False, None
        _FakeVJob.made.append(self)

    def assign(self, p):
        self.argv = list(p.args)
        if _FakeVJob.fail_assign:
            self.proc = p
            raise OSError("fake: AssignProcessToJobObject failed")
        _time.sleep(0.3)
        self.waiting = p.poll() is None          # still alive = still waiting for "go"
        self.proc = p

    def terminate(self, code):
        self.term = code
        self.proc.kill()

    def close(self):
        self.closed = True


tool._JOB_FACTORY = _FakeVJob
try:
    _good = tmp / "average.py"
    _good.write_text((kit / "tasks" / "bugfix" / "selftest" / "good.py").read_text())
    v, text, p, n = bm.judge(_bf, _good, tool)
    _j = _FakeVJob.made[-1] if _FakeVJob.made else None
    check(v == "PASS" and _j and _j.args == (bm.VERIFY_MEM_MB, bm.VERIFY_TIMEOUT, bm.VERIFY_PROCS) and _j.closed and _j.waiting,
          "W4 verifier job path: the good file PASSes inside the job (memory cap, a few processes), the job is closed after",
          f"{v} {getattr(_j, 'args', None)} {text[-120:]}")
    check(_j and bm._WAIT_THEN_RUN in _j.argv, "W4 verifier job path: the verifier is started through the 'go' wrapper (nothing runs before the job)")
    _FakeVJob.fail_assign = True
    v, text, p, n = bm.judge(_bf, _good, tool)
    _FakeVJob.fail_assign = False
    check(v == "FAIL" and "could not be started inside its job" in text and _FakeVJob.made[-1].proc.poll() is not None
          and _FakeVJob.made[-1].closed, "W4 verifier job path: if the verifier cannot be put in its job it is killed and nothing is judged", text[:120])
    _old_vt = bm.VERIFY_TIMEOUT
    bm.VERIFY_TIMEOUT = 3
    _good.write_text("import time\ntime.sleep(60)\n")
    v, text, p, n = bm.judge(_bf, _good, tool)
    bm.VERIFY_TIMEOUT = _old_vt
    check(v == "FAIL" and "timed out" in text and _FakeVJob.made[-1].term == 124 and _FakeVJob.made[-1].closed,
          "W4 verifier job path: a verifier that runs too long is ended by the job (code 124) and recorded as FAIL", text[:120])
finally:
    tool._JOB_FACTORY = None
_r = subprocess.run([sys.executable, "-I", "-c", bm._WAIT_THEN_RUN, str(kit / "tasks" / "bugfix" / "verify.py"),
                     str(tmp / "average.py")], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
check(_r.returncode == 126 and "VERIFY" not in _r.stdout, "W4 the verifier wrapper runs NOTHING unless it reads exactly 'go' (end-of-file -> 126)", f"exit {_r.returncode}")
# a model-written file that hangs is stopped by the verifier's timeout
bm.VERIFY_TIMEOUT = 3
hang = tmp / "hang.py"
hang.write_text("while True:\n    pass\n")
v, text, p, n = bm.judge(bm.all_tasks()["wordcount"], hang, None)
check(v == "FAIL", "a hanging file cannot hang the verifier", text[:150])

if os.environ.get("KEEP"):
    print("kept:", tmp)
else:
    shutil.rmtree(tmp, ignore_errors=True)
print("\nALL OK" if not bad else f"\n{bad} CHECK(S) FAILED")
sys.exit(1 if bad else 0)
