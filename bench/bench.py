#!/usr/bin/env python3
"""bench.py - the testkit's task bank, automatic capture, notes, reports and training export.

Everything lives next to this file:
  tasks/<name>/   task.json, prompt.txt, verify.py, optional files/ (copied into the sandbox), selftest/
  runs/<session>/ what one `run` captured: meta.json, notes.md, report.txt, <task>/run_<i>/...
  reports/        a flat copy of every session's report.txt (the file you hand to an agent)
  exports/        training files written by `export`

Commands (python bench.py <command> -h for options):
  list                         the tasks
  selftest [task ...]          check the VERIFIERS themselves: known-good files must PASS, known-bad must FAIL
  run [-t task ...] [-n N]     run the agent N times per task, judge each run, capture everything, write the report
  note "text" [--run task/i]   add your own note to the latest session (or to one run); safe to do while a run is going
  report [session]             rebuild the report (picks up new notes)
  show task/i [session]        print one run the way you used to with  cat run.log && cat skill.py && cat verify.log
  stats                        pass rates of all sessions side by side
  export [-o file]             verified passes -> JSONL for fine-tuning (see the warning it prints)

Only Python 3 and the standard library are needed. The verifiers run model-written code OUTSIDE the
sandbox guard (with a timeout and CPU/file-size limits, after the tool's own pre-scan). They are not a
sandbox: only run this against a model you are willing to run code from.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parent
TASKS, RUNS, WORK = KIT / "tasks", KIT / "runs", KIT / "work"
REPORTS, EXPORTS = KIT / "reports", KIT / "exports"
VERIFY_TIMEOUT = 90
RUN_TIMEOUT = int(os.environ.get("BENCH_RUN_TIMEOUT", "3600"))


# ----------------------------------------------------------------------------- small helpers
def sha(text_or_bytes) -> str:
    b = text_or_bytes if isinstance(text_or_bytes, bytes) else str(text_or_bytes).encode()
    return hashlib.sha256(b).hexdigest()[:10]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30]


def read(p, default=""):
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return default


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def tools_dir(arg=None) -> Path:
    """Folder holding sandbox_tool.py: --tools, $TOOLS, ../src (a repo checkout), or the installed package."""
    cands = [arg, os.environ.get("TOOLS"), KIT.parent / "src"]
    spec = importlib.util.find_spec("sandbox_tool") if importlib.util.find_spec("sandbox_tool") else None
    if spec and spec.origin:
        cands.append(Path(spec.origin).parent)
    for cand in cands:
        if cand and (Path(cand) / "sandbox_tool.py").is_file():
            return Path(cand).resolve()
    die("STOP: sandbox_tool.py not found. Use --tools <folder>, set TOOLS=<folder>, run from a repo checkout, "
        "or `pip install` the package.")


_TOOL_CACHE = {}


def load_tool(tools: Path):
    """Import sandbox_tool.py (for SYSTEM and scan_python only). Nothing is executed beyond its definitions."""
    key = str(tools)
    if key not in _TOOL_CACHE:
        old = os.environ.get("SANDBOX_DIR")
        os.environ["SANDBOX_DIR"] = tempfile.mkdtemp(prefix="bench_import_")
        try:
            spec = importlib.util.spec_from_file_location("sandbox_tool_for_bench", tools / "sandbox_tool.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _TOOL_CACHE[key] = mod
        finally:
            shutil.rmtree(os.environ["SANDBOX_DIR"], ignore_errors=True)
            if old is None:
                os.environ.pop("SANDBOX_DIR", None)
            else:
                os.environ["SANDBOX_DIR"] = old
    return _TOOL_CACHE[key]


# ----------------------------------------------------------------------------- tasks
class Task:
    def __init__(self, d: Path):
        self.dir, self.name = d, d.name
        self.cfg = json.loads(read(d / "task.json", "{}") or "{}")
        self.prompt = read(d / "prompt.txt").strip()
        self.verify = d / "verify.py"
        self.output = self.cfg.get("output", "skill.py")
        # several files the model may edit (multi-file tasks); the first one is the file the verifier is given.
        # Single-file tasks leave this out and behave exactly as before.
        self.outputs = list(dict.fromkeys([self.output, *self.cfg.get("outputs", [])]))
        self.title = self.cfg.get("title", self.name)
        self.max_steps = int(self.cfg.get("max_steps", 10))
        self.difficulty = self.cfg.get("difficulty", "?")
        self.fixtures = d / "files"
        # optional (update 25): the lines the program must print; the tool then refuses DONE until a run prints them
        self.expect = d / "expected_output.txt" if (d / "expected_output.txt").is_file() else None
        # optional (W10): a test file among the task's files; the tool keeps it read-only and refuses DONE until a
        # run of it passes after the last code change
        self.test_file = self.cfg.get("test_file") or None
        if self.test_file and not (self.fixtures / self.test_file).is_file():
            raise SystemExit(f"task {self.name}: test_file {self.test_file} is not in its files/ folder")


def all_tasks() -> dict:
    return {d.name: Task(d) for d in sorted(TASKS.iterdir())
            if d.is_dir() and not d.name.startswith("_") and (d / "task.json").is_file()}


def pick_tasks(names) -> list:
    every = all_tasks()
    if not names or names == ["all"]:
        return list(every.values())
    out = []
    for n in names:
        if n not in every:
            die(f"STOP: no task called {n!r}. Tasks: {', '.join(every)}")
        out.append(every[n])
    return out


# ----------------------------------------------------------------------------- judging
def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024 * 1024,) * 2)
    except Exception:
        pass


# Windows (W4): the verifier runs the model's final files, so it runs inside a Job Object like run_python. It waits for
# "go" so nothing runs before it is inside the job; anything else on stdin (end-of-file) = run nothing.
_WAIT_THEN_RUN = ("import sys, runpy\n"
                  "if sys.stdin.readline() != 'go' + chr(10):\n"
                  "    sys.exit(126)\n"
                  "sys.argv = sys.argv[1:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
VERIFY_MEM_MB = 1024
VERIFY_PROCS = 4          # the verifier + the `python main.py` it starts for the output check, with room to spare


def _run_verifier(task: Task, local: Path, d: str, tool):
    """Run verify.py on the copied files. POSIX: CPU / file-size limits (preexec). Windows: inside a Job Object
    (memory cap, a few processes, killed on timeout). Returns a CompletedProcess; raises TimeoutExpired."""
    argv = [str(task.verify), str(local)]
    make = getattr(tool, "_JOB_FACTORY", None) if tool is not None else None
    if make is None and os.name == "posix":
        return subprocess.run([sys.executable, "-I", *argv], cwd=d, capture_output=True, text=True,
                              timeout=VERIFY_TIMEOUT, preexec_fn=_limits)
    if make is None and tool is not None and getattr(tool, "_has_job_objects", lambda: False)():
        make = tool._WinJob
    if make is None:          # no way to limit it here: as before (timeout only)
        return subprocess.run([sys.executable, "-I", *argv], cwd=d, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
    exe = tool._job_python() if hasattr(tool, "_job_python") else sys.executable
    job = make(VERIFY_MEM_MB, VERIFY_TIMEOUT, procs=VERIFY_PROCS)
    try:
        p = subprocess.Popen([exe, "-I", "-c", _WAIT_THEN_RUN, *argv], cwd=d, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            job.assign(p)
        except OSError:
            p.kill()
            p.wait()
            raise
        try:   # sends "go" and closes stdin itself (closing it by hand breaks communicate() before Python 3.13)
            out, _ = p.communicate(input="go\n", timeout=VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.terminate(124)
            p.wait()
            raise
        return subprocess.CompletedProcess(p.args, p.returncode, out, "")
    finally:
        job.close()


def judge(task: Task, outfile: Path, tool=None):
    """Run the task's verifier on the output file. Returns (verdict, text, passed, total).

    Multi-file tasks: every file in task.outputs is taken from outfile's folder, scanned, and copied next to it,
    so the verifier can import the modules it needs."""
    if not outfile.is_file():
        return "FAIL", f"no output file written: expected {task.output}\nVERIFY: FAIL (0/1)\n", 0, 1
    files = [outfile] + [outfile.parent / n for n in task.outputs[1:] if (outfile.parent / n).is_file()]
    for f in files:
        if tool is not None and f.suffix == ".py":
            try:
                probs = tool.scan_python(read(f))
            except Exception:
                probs = []
            if probs:
                return ("FAIL", f"the verifier did NOT run {f.name}: it contains constructs the sandbox blocks:\n"
                        + "\n".join("  " + p for p in probs[:10]) + "\nVERIFY: FAIL (0/1)\n", 0, 1)
    with tempfile.TemporaryDirectory(prefix="bench_verify_") as d:
        local = Path(d) / outfile.name
        for f in files:
            shutil.copy(f, Path(d) / f.name)
        # W10: the task's test file goes along as data, so the verifier can check it was not changed (never run here)
        if task.test_file and (outfile.parent / task.test_file).is_file():
            shutil.copy(outfile.parent / task.test_file, Path(d) / task.test_file)
        try:
            p = _run_verifier(task, local, d, tool)
            text = (p.stdout or "") + (p.stderr or "")
        except OSError as e:
            return "FAIL", f"the verifier could not be started inside its job, so nothing was run: {e}\nVERIFY: FAIL (0/1)\n", 0, 1
        except subprocess.TimeoutExpired:
            return "FAIL", f"verifier timed out after {VERIFY_TIMEOUT}s\nVERIFY: FAIL (0/1)\n", 0, 1
    last = next((l for l in reversed(text.strip().splitlines()) if l.strip()), "")
    m = re.match(r"VERIFY: (PASS|FAIL) \((\d+)/(\d+)\)", last)
    if not m:
        return "FAIL", text + "\n(verifier printed no 'VERIFY:' line, so this counts as FAIL)\nVERIFY: FAIL (0/1)\n", 0, 1
    return m.group(1), text, int(m.group(2)), int(m.group(3))


# ----------------------------------------------------------------------------- sessions
def sessions() -> list:
    return sorted(d for d in RUNS.glob("*") if (d / "meta.json").is_file()) if RUNS.is_dir() else []


def find_session(arg=None) -> Path:
    ss = sessions()
    if not ss:
        die("STOP: no sessions yet. Run:  python bench.py run -t all -n 3")
    if not arg or arg == "latest":
        return ss[-1]
    hits = [s for s in ss if s.name == arg] or [s for s in ss if s.name.startswith(arg)] or [s for s in ss if arg in s.name]
    if not hits:
        die(f"STOP: no session matching {arg!r}. Sessions: {', '.join(s.name for s in ss[-8:])}")
    return hits[-1]


def new_session(label: str) -> Path:
    RUNS.mkdir(exist_ok=True)
    base = datetime.datetime.now().strftime("%Y%m%d-%H%M") + (f"-{slug(label)}" if label else "")
    name, k = base, 1
    while (RUNS / name).exists():
        k += 1
        name = f"{base}-{k}"
    (RUNS / name).mkdir()
    return RUNS / name


def server_model() -> str:
    if os.environ.get("BACKEND", "local") != "local":
        return os.environ.get("GEMINI_MODEL", "gemini")
    url = os.environ.get("LLAMA_URL") or "http://localhost:8080/v1/chat/completions"
    try:
        with urllib.request.urlopen(url.split("/v1/")[0] + "/v1/models", timeout=3) as r:
            return json.load(r)["data"][0]["id"]
    except Exception:
        return "unknown (server did not answer /v1/models)"


def add_note(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"[{datetime.datetime.now():%H:%M:%S}] {text.strip()}\n")


# ----------------------------------------------------------------------------- one run
def parse_log(log: str, transcript: dict) -> dict:
    cmds = {}
    for m in (transcript or {}).get("messages", []):
        if m.get("role") != "assistant":
            continue
        c = re.search(r"^\s*(CMD|DONE):\s*(\S*)", m["content"], re.M)
        if c:
            k = "DONE" if c.group(1) == "DONE" else (c.group(2) or "?")
            cmds[k] = cmds.get(k, 0) + 1
    if not cmds:
        for c in re.findall(r"^model> (?:.*\n)*?CMD:\s*(\S+)", log, re.M):
            cmds[c] = cmds.get(c, 0) + 1
    speeds = [float(x) for x in re.findall(r"generated \d+ tok @ ([\d.]+) tok/s", log)]
    final = re.search(r"^final> (.*)$", log, re.M)
    if final:
        ended = "stopped: " + re.sub(r"^\(stopped:\s*|\)$", "", final.group(1)).strip()
    elif re.search(r"^model> (?:.*\n)*?\s*DONE:", log, re.M) or "DONE:" in log:
        ended = "done"
    else:
        ended = "unknown"
    return {
        "steps": len(re.findall(r"^model>", log, re.M)),
        "cmds": cmds,
        "ended": ended,
        "refused": len(re.findall(r"^tool>\s+REFUSED", log, re.M)),
        "expect_blocks": len(re.findall(r"^tool>\s+REFUSED: cannot accept DONE yet:.*different from the expected output", log, re.M)),
        "test_blocks": len(re.findall(r"^tool>\s+REFUSED: cannot accept DONE yet:.*(?:the task's tests|after the task's tests)", log, re.M)),
        "gen_tok_s": round(sum(speeds) / len(speeds), 2) if speeds else None,
    }


def run_one(sess: Path, task: Task, i: int, tools: Path, live: bool, keep: bool, tool) -> dict:
    rd = sess / task.name / f"run_{i}"
    rd.mkdir(parents=True, exist_ok=True)
    work = WORK / f"{sess.name}_{task.name}_{i}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if task.fixtures.is_dir():
        shutil.copytree(task.fixtures, work, dirs_exist_ok=True)
    env = dict(os.environ, SANDBOX_DIR=str(work), AGENT_TRANSCRIPT=str(rd / "transcript.json"),
               AUDIT_LOG=str(rd / "refused.log"), AGENT_MAX_STEPS=str(task.max_steps), PYTHONUNBUFFERED="1")
    env.pop("AGENT_EXPECT_FILE", None)          # only a task's own expected_output.txt may switch the gate on
    if task.expect:
        env["AGENT_EXPECT_FILE"] = str(task.expect)
    env.pop("AGENT_TEST_FILE", None)            # W10: only a task's own test_file switches the test gate on
    if task.test_file:
        env["AGENT_TEST_FILE"] = task.test_file
    cmd = [sys.executable, str(tools / "sandbox_tool.py"), "--agent", task.prompt]
    t0, lines = time.time(), []
    proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1, start_new_session=(os.name == "posix"))
    timer = threading.Timer(RUN_TIMEOUT, lambda: (proc.kill(), lines.append(f"\n[bench] killed: run exceeded {RUN_TIMEOUT}s\n")))
    timer.start()
    try:
        for line in proc.stdout:
            lines.append(line)
            if live:
                sys.stdout.write("    | " + line)
                sys.stdout.flush()
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        lines.append("\n[bench] interrupted by you (Ctrl-C)\n")
        raise
    finally:
        timer.cancel()
        secs = round(time.time() - t0)
        log = "".join(lines)
        (rd / "run.log").write_text(log)
        for name in task.outputs:             # the file(s) the verifier judges, as the model left them
            if (work / name).is_file():
                shutil.copy(work / name, rd / name)
        if task.test_file and (work / task.test_file).is_file():   # W10.1: the test file as the run left it, so the
            shutil.copy(work / task.test_file, rd / task.test_file)  # verifier can check it is unchanged
        for extra in work.glob("*"):          # keep any other file the model made (small ones only)
            if extra.is_file() and extra.name not in (*task.outputs, "file_index.txt") and extra.stat().st_size < 200_000 \
                    and not (task.fixtures / extra.name).exists():
                shutil.copy(extra, rd / ("extra_" + extra.name))
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    verdict, vtext, passed, total = judge(task, rd / task.output, tool)
    (rd / "verify.log").write_text(vtext)
    transcript = {}
    try:
        transcript = json.loads(read(rd / "transcript.json", "{}") or "{}")
    except ValueError:
        pass
    info = parse_log(log, transcript)
    res = {"task": task.name, "run": i, "verify": verdict, "passed": passed, "total": total, "seconds": secs,
           "failed_checks": [re.split(r"\s+->\s+", l[6:].strip())[0] for l in vtext.splitlines() if l.startswith("FAIL  ")],
           "said_done": info["ended"] == "done", **info}
    res["claimed_false"] = res["said_done"] and verdict != "PASS"
    (rd / "result.json").write_text(json.dumps(res, indent=1))
    return res


# ----------------------------------------------------------------------------- report
def load_results(sess: Path) -> list:
    out = []
    for f in sorted(sess.glob("*/run_*/result.json"), key=lambda p: (p.parts[-3], int(p.parts[-2].split("_")[1]))):
        try:
            out.append(json.loads(read(f)))
        except ValueError:
            pass
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def build_report(sess: Path, detail: str = "all") -> str:
    meta = json.loads(read(sess / "meta.json", "{}") or "{}")
    results = load_results(sess)
    o = []
    w = o.append
    w("=" * 72)
    w(f"BENCH REPORT  {sess.name}" + (f"   [{meta.get('label')}]" if meta.get("label") else ""))
    w("=" * 72)
    w("SETUP")
    for k in ("started", "finished", "backend", "model", "python", "tool_sha", "system_sha", "runs_per_task", "tools_dir"):
        if k in meta:
            w(f"  {k:14s} {meta[k]}")
    for name, t in meta.get("tasks", {}).items():
        w(f"  task {name:12s} prompt {t['prompt_sha']}  verifier {t['verify_sha']}  ({t['title']})")
        if t.get("expect_sha"):
            w(f"       {'':12s} EXPECTED-OUTPUT GATE ON (expected_output {t['expect_sha']}): DONE needs a run that prints it")
        if t.get("test_sha"):
            w(f"       {'':12s} TEST GATE ON ({t['test_file']} {t['test_sha']}): DONE needs a passing run of it after the last change")
    w("")
    w("SUMMARY (one row per task)")
    w(f"  {'task':12s} {'pass':>9s} {'false-DONE':>10s} {'steps':>6s} {'secs':>6s} {'tok/s':>6s} {'refused':>8s}")
    tot_p = tot_n = 0
    fails = {}
    for name in dict.fromkeys(r["task"] for r in results):
        rs = [r for r in results if r["task"] == name]
        p = sum(r["verify"] == "PASS" for r in rs)
        tot_p, tot_n = tot_p + p, tot_n + len(rs)
        w(f"  {name:12s} {f'{p}/{len(rs)}':>9s} {sum(r['claimed_false'] for r in rs):>10d} {_avg([r['steps'] for r in rs])!s:>6s} "
          f"{_avg([r['seconds'] for r in rs])!s:>6s} {_avg([r['gen_tok_s'] for r in rs])!s:>6s} {sum(r['refused'] for r in rs):>8d}")
        for r in rs:
            for fc in r["failed_checks"]:
                fails.setdefault(name, {}).setdefault(fc, []).append(r["run"])
    w(f"  {'TOTAL':12s} {f'{tot_p}/{tot_n}':>9s}")
    w("  false-DONE = the tool accepted the model's DONE but the verifier said FAIL (the model believed it was finished).")
    if fails:
        w("")
        w("MOST COMMON FAILED CHECKS")
        for name, d in fails.items():
            for fc, runs in sorted(d.items(), key=lambda kv: -len(kv[1]))[:6]:
                w(f"  {name}: {fc}   (runs {', '.join(map(str, runs))})")
    ended = {}
    for r in results:
        ended[r["ended"].split(":")[1].strip()[:50] if r["ended"].startswith("stopped") else r["ended"]] = \
            ended.get(r["ended"].split(":")[1].strip()[:50] if r["ended"].startswith("stopped") else r["ended"], 0) + 1
    if ended:
        w("")
        w("HOW RUNS ENDED  " + "; ".join(f"{k}: {v}" for k, v in ended.items()))
    gated = [n for n, t in meta.get("tasks", {}).items() if t.get("expect_sha")]
    for n in gated:
        rs = [r for r in results if r["task"] == n]
        hit = [r for r in rs if r.get("expect_blocks")]
        w(f"EXPECTED-OUTPUT GATE ({n})  refused a DONE on wrong output {sum(r.get('expect_blocks', 0) for r in rs)} time(s), "
          f"in {len(hit)} of {len(rs)} run(s)" + (f": runs {', '.join(str(r['run']) for r in hit)}" if hit else ""))
    for n in [n for n, t in meta.get("tasks", {}).items() if t.get("test_sha")]:
        rs = [r for r in results if r["task"] == n]
        hit = [r for r in rs if r.get("test_blocks")]
        w(f"TEST GATE ({n})  refused a DONE because the task's tests had not passed {sum(r.get('test_blocks', 0) for r in rs)} "
          f"time(s), in {len(hit)} of {len(rs)} run(s)" + (f": runs {', '.join(str(r['run']) for r in hit)}" if hit else ""))
    notes = read(sess / "notes.md").strip()
    w("")
    w("YOUR NOTES (python bench.py note \"text\")")
    w(notes if notes else "  (none yet)")
    w("")
    w("RUNS")
    for r in results:
        rd = sess / r["task"] / f"run_{r['run']}"
        w("")
        w("-" * 72)
        w(f"RUN {r['task']}/{r['run']}   {r['verify']} ({r['passed']}/{r['total']})   steps {r['steps']}   "
          f"{r['seconds']}s   ended: {r['ended']}" + ("   ** CLAIMED DONE BUT FAILED **" if r["claimed_false"] else ""))
        w("commands used: " + (", ".join(f"{k} x{v}" for k, v in r["cmds"].items()) or "none") +
          f"   refused: {r['refused']}" + (f"   expected-output gate refused DONE: {r['expect_blocks']}" if r.get("expect_blocks") else "")
          + (f"   test gate refused DONE: {r['test_blocks']}" if r.get("test_blocks") else ""))
        rn = read(rd / "notes.md").strip()
        if rn:
            w("your notes on this run:\n" + rn)
        show = detail == "all" or (detail == "fail" and r["verify"] != "PASS")
        if not show:
            continue
        w(f"----- run log ({r['task']}/{r['run']}) -----")
        w(read(rd / "run.log").rstrip())
        outnames = sorted(p.name for p in rd.glob("*") if p.suffix == ".py" and not p.name.startswith("extra_"))
        for outname in outnames:
            w(f"----- {outname} (the file the model wrote) -----")
            w(read(rd / outname).rstrip())
        if not outnames:
            w("----- (the model wrote no output file) -----")
        for p in sorted(rd.glob("extra_*")):
            w(f"----- {p.name[6:]} (extra file the model made) -----")
            w(read(p).rstrip())
        w("----- verify log -----")
        w(read(rd / "verify.log").rstrip())
        if read(rd / "refused.log").strip():
            w("----- refused.log (what the tool refused) -----")
            w(read(rd / "refused.log").rstrip())
    return "\n".join(o) + "\n"


def write_report(sess: Path, detail="all") -> Path:
    text = build_report(sess, detail)
    (sess / "report.txt").write_text(text)
    REPORTS.mkdir(exist_ok=True)
    shutil.copy(sess / "report.txt", REPORTS / f"{sess.name}.txt")
    return REPORTS / f"{sess.name}.txt"


# ----------------------------------------------------------------------------- commands
def cmd_list(a):
    print(f"{'task':12s} {'level':>5s}  {'output':12s} title")
    for t in all_tasks().values():
        extra = "  (+fixture files)" if t.fixtures.is_dir() else ""
        print(f"{t.name:12s} {t.difficulty!s:>5s}  {t.output:12s} {t.title}{extra}")


def cmd_selftest(a):
    tools = tools_dir(a.tools)
    tool = load_tool(tools)
    bad = 0
    for t in pick_tasks(a.task):
        # a selftest entry is one .py file, or (multi-file tasks) a folder holding the task's output files
        entries = [(f.name, f) for f in sorted((t.dir / "selftest").glob("*.py"))]
        entries += [(d.name + "/", d / t.output) for d in sorted((t.dir / "selftest").iterdir()) if d.is_dir()]
        for name, f in entries:
            want = "PASS" if name.startswith("good") else "FAIL"
            v, text, p, n = judge(t, f, tool)
            ok = v == want
            bad += not ok
            print(f"{'ok  ' if ok else 'WRONG'} {t.name:10s} {name:28s} verifier says {v} ({p}/{n}), should be {want}")
            if not ok:
                print("      " + text.strip().replace("\n", "\n      ")[:600])
    print("\nALL VERIFIERS BEHAVE" if not bad else f"\n{bad} verifier check(s) are WRONG: do not trust runs of those tasks")
    sys.exit(1 if bad else 0)


def cmd_run(a):
    tools = tools_dir(a.tools)
    tool = load_tool(tools)
    tasks = pick_tasks(a.task)
    for t in tasks:
        if not t.prompt or not t.verify.is_file():
            die(f"STOP: task {t.name} has no prompt.txt or verify.py. Nothing was run.")
    if "AGENT_TRANSCRIPT" not in read(tools / "sandbox_tool.py"):
        print("WARNING: this sandbox_tool.py is older than the testkit: conversations will not be saved (so `export` finds nothing)\n"
              "         and per-task step limits are ignored. Copy the newest sandbox_tool.py into " + str(tools) + "\n")
    sess = new_session(a.label)
    meta = {"id": sess.name, "label": a.label or "", "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "backend": os.environ.get("BACKEND", "local"), "model": server_model(),
            "python": sys.version.split()[0], "tool_sha": sha((tools / "sandbox_tool.py").read_bytes()),
            "system_sha": sha(getattr(tool, "SYSTEM", "")), "runs_per_task": a.n, "tools_dir": str(tools),
            "tasks": {t.name: {"prompt_sha": sha(t.prompt), "verify_sha": sha(read(t.verify)), "title": t.title,
                               "expect_sha": sha(read(t.expect)) if t.expect else None,
                               "test_file": t.test_file,
                               "test_sha": sha(read(t.fixtures / t.test_file)) if t.test_file else None} for t in tasks}}
    (sess / "meta.json").write_text(json.dumps(meta, indent=1))
    if a.note:
        add_note(sess / "notes.md", a.note)
    print(f"session {sess.name}: {len(tasks)} task(s) x {a.n} run(s)   model: {meta['model']}   tool {meta['tool_sha']}   "
          f"SYSTEM {meta['system_sha']}")
    print("add notes any time from another terminal:  python bench.py note \"your text\"\n")
    done = fails = 0
    try:
        for t in tasks:
            for i in range(1, a.n + 1):
                print(f"[{done + 1}/{len(tasks) * a.n}] {t.name} run {i} ...", flush=True)
                r = run_one(sess, t, i, tools, a.live, a.keep_work, tool)
                done += 1
                fails += r["verify"] != "PASS"
                print(f"    {r['verify']} ({r['passed']}/{r['total']})  steps {r['steps']}  {r['seconds']}s  "
                      f"ended: {r['ended']}" + ("   ** CLAIMED DONE BUT FAILED **" if r["claimed_false"] else ""), flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted: saving what was captured so far")
    meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    (sess / "meta.json").write_text(json.dumps(meta, indent=1))
    rep = write_report(sess, a.detail)
    res = load_results(sess)
    print(f"\nverified PASS: {sum(r['verify'] == 'PASS' for r in res)} / {len(res)}     "
          f"claimed DONE but verify FAILED: {sum(r['claimed_false'] for r in res)}")
    print(f"report (hand this one file to an agent): {rep}")


def cmd_note(a):
    sess = find_session(a.session)
    if a.run:
        t, _, i = a.run.partition("/")
        if not (sess / t / f"run_{i}").is_dir():
            die(f"STOP: no run {a.run} in session {sess.name}")
        add_note(sess / t / f"run_{i}" / "notes.md", a.text)
    else:
        add_note(sess / "notes.md", a.text)
    print(f"noted in {sess.name}" + (f" ({a.run})" if a.run else "") + ". `python bench.py report` rebuilds the report.")


def cmd_report(a):
    sess = find_session(a.session)
    print(f"report: {write_report(sess, a.detail)}")


def cmd_show(a):
    sess = find_session(a.session)
    t, _, i = a.run.partition("/")
    rd = sess / t / f"run_{i}"
    if not rd.is_dir():
        die(f"STOP: no run {a.run} in session {sess.name}")
    print(read(rd / "run.log"))
    for p in sorted(rd.glob("*.py")):
        print(f"----- {p.name} -----\n{read(p)}")
    print("----- verify -----\n" + read(rd / "verify.log"))


def cmd_stats(a):
    print(f"{'session':34s} {'tool':10s} {'SYSTEM':10s} per-task pass")
    for s in sessions():
        meta = json.loads(read(s / "meta.json", "{}") or "{}")
        res = load_results(s)
        parts = []
        for name in dict.fromkeys(r["task"] for r in res):
            rs = [r for r in res if r["task"] == name]
            parts.append(f"{name} {sum(r['verify'] == 'PASS' for r in rs)}/{len(rs)}")
        print(f"{s.name:34s} {meta.get('tool_sha', '?'):10s} {meta.get('system_sha', '?'):10s} " + "  ".join(parts))
    print("\n(Same tool and SYSTEM hash = the code and prompt did not change between those sessions.)")


def cmd_export(a):
    ss = [find_session(s) for s in a.session] if a.session else sessions()
    seen, rows, per = set(), [], {}
    for s in ss:
        meta = json.loads(read(s / "meta.json", "{}") or "{}")
        for r in load_results(s):
            if r["verify"] != "PASS" or r["ended"] != "done":
                continue
            tpath = s / r["task"] / f"run_{r['run']}" / "transcript.json"
            try:
                tr = json.loads(read(tpath))
            except ValueError:
                continue
            msgs = [{"role": m["role"], "content": m["content"]} for m in tr.get("messages", [])]
            if a.no_system:
                msgs = [m for m in msgs if m["role"] != "system"]
            key = sha(json.dumps(msgs))
            if key in seen or per.get(r["task"], 0) >= a.max_per_task:
                continue
            seen.add(key)
            per[r["task"]] = per.get(r["task"], 0) + 1
            rows.append({"messages": msgs, "task": r["task"], "session": s.name, "run": r["run"],
                         "checks": f"{r['passed']}/{r['total']}", "tool_sha": meta.get("tool_sha"),
                         "system_sha": meta.get("system_sha")})
    EXPORTS.mkdir(exist_ok=True)
    out = Path(a.out) if a.out else EXPORTS / f"verified_{datetime.datetime.now():%Y%m%d-%H%M}.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"wrote {len(rows)} verified examples to {out}")
    for k, v in per.items():
        print(f"  {k}: {v}")
    print("\nWARNING: this is a seed, not a training set. A few dozen examples of a handful of tasks will mostly teach a\n"
          "model to repeat those tasks. Plan on many different tasks (hundreds of verified examples) before fine-tuning,\n"
          "keep a held-out set of tasks you never train on, and compare scores with `bench.py run` before and after.")


def main():
    ap = argparse.ArgumentParser(description="testkit task bank, capture and reports", formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.set_defaults(f=cmd_list)
    s = sub.add_parser("selftest"); s.add_argument("task", nargs="*"); s.add_argument("--tools"); s.set_defaults(f=cmd_selftest)
    s = sub.add_parser("run")
    s.add_argument("-t", "--task", nargs="*", default=["all"], help="task names (default: all)")
    s.add_argument("-n", type=int, default=3, help="runs per task (default 3)")
    s.add_argument("--label", default="", help="short name for this session, e.g. 'import-hint'")
    s.add_argument("--note", default="", help="a first note for the session")
    s.add_argument("--tools", help="folder holding sandbox_tool.py")
    s.add_argument("--live", action="store_true", help="echo the agent's output as it runs")
    s.add_argument("--keep-work", action="store_true", help="do not delete the per-run sandbox folder")
    s.add_argument("--detail", choices=["all", "fail", "none"], default="all", help="how much of each run goes in the report")
    s.set_defaults(f=cmd_run)
    s = sub.add_parser("note"); s.add_argument("text"); s.add_argument("--run", help="task/number, e.g. wordcount/3")
    s.add_argument("--session"); s.set_defaults(f=cmd_note)
    s = sub.add_parser("report"); s.add_argument("session", nargs="?")
    s.add_argument("--detail", choices=["all", "fail", "none"], default="all"); s.set_defaults(f=cmd_report)
    s = sub.add_parser("show"); s.add_argument("run", help="task/number"); s.add_argument("session", nargs="?"); s.set_defaults(f=cmd_show)
    s = sub.add_parser("stats"); s.set_defaults(f=cmd_stats)
    s = sub.add_parser("export"); s.add_argument("-o", "--out"); s.add_argument("--session", nargs="*")
    s.add_argument("--no-system", action="store_true", help="leave the long system prompt out of each example")
    s.add_argument("--max-per-task", type=int, default=10**9, help="cap examples per task so one task cannot dominate")
    s.set_defaults(f=cmd_export)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
