# bench: task bank, capture, reports

Run from a checkout of the repository (`python bench/bench.py ...`). Needs only Python 3 and the standard library.
It finds `sandbox_tool.py` in `../src`, or in the installed package, or from `--tools <folder>` / `TOOLS=<folder>`.

```bash
python bench/test_bench.py        # must print ALL OK (fake model server, about a minute)
python bench/bench.py selftest    # must print ALL VERIFIERS BEHAVE (tests the checkers themselves)
python bench/bench.py list
python bench/bench.py run -t wordcount stack -n 3 --label first-try
python bench/bench.py run -t all -n 3 --live
```

Point it at your server first (`LLAMA_URL`, `BACKEND`, ... as in the main README). Every run uses a fresh empty
sandbox folder (plus the task's fixture files), so leftovers cannot help or hurt.

## What gets written

`bench/runs/<session>/<task>/run_<n>/` holds `run.log`, the file the model wrote, `verify.log`,
`transcript.json` (the whole conversation), `result.json` (verdict, checks passed, steps, commands used, seconds,
how it ended, generation speed, failed check names, and `claimed_false`: the tool accepted the model's DONE but
the verifier said FAIL) and `refused.log`. `meta.json` records the model name, hashes of the tool, the system
prompt and each task's prompt and verifier. When a run finishes you get one report file,
`bench/reports/<session>.txt`: summary table, your notes, then every run's log, file and verify log.

## Commands

| Command | |
|---|---|
| `run -t <tasks> -n N --label X --note "..."` | Run, judge, capture, write the report. `--live` echoes the agent's output. |
| `note "text" [--run task/3]` | Add a note to the latest session (or one run), even while a run is going. |
| `report [session] [--detail all\|fail\|none]` | Rebuild the report (picks up new notes). Edit `notes.md` files, never `report.txt`. |
| `show task/3 [session]` | Print one run: log, file, verify log. |
| `stats` | Pass rates per task for every session, with the tool and prompt hashes. |
| `export [-o file] [--no-system] [--max-per-task N]` | Verified passes as JSONL (chat messages). A seed for fine-tuning, not a training set. |
| `selftest [task]` | Every verifier must pass its `selftest/good*` and fail its `selftest/bad*` (a `.py` file, or a folder for multi-file tasks). |

## Keeping results honest

* A run is PASS only if the verifier printed `VERIFY: PASS`. A missing output file, a dead server, a blocked
  construct, a hang or a crash is recorded as FAIL with the reason.
* It refuses to start on a missing or empty prompt.
* The verifier will not execute a file that contains constructs the sandbox blocks. It does run the model's file
  **outside** the sandbox guard (timeout, CPU and file-size limits only), so it is not a sandbox: only run it
  against models you are willing to run code from.

## Adding a task

`bench/tasks/<name>/` needs `task.json` (`title`, `output`, `difficulty`, `max_steps`), `prompt.txt`, `verify.py`
(prints PASS/FAIL lines and a last line `VERIFY: PASS (n/n)`; copy `tasks/stack/verify.py`), optional `files/`
(copied into the sandbox before the run) and `selftest/` references. Write the verifier so a hard-coded answer
fails (see `csv_total`, which tests with different data).

**Multi-file tasks** (e.g. `pipeline_omni`): add `"outputs": ["calc.py", "ingest.py"]` to `task.json`. `output` stays
the file the verifier is given; every file in `outputs` is kept in the run folder as the model left it, scanned for
blocked constructs, shown in the report, and copied next to `output` for the verifier. A selftest entry is then a
folder (`selftest/good/`, `selftest/bad_x/`) holding the task's files. Tasks without `outputs` behave as before.

`pipeline_omni` (difficulty 5, 14 steps): three given files, three bugs found in order: a TypeError that shows up in
`calc.py` but comes from `ingest.py` (prices left as strings), a ZeroDivisionError in `calc.py` on no rows, and a
filter that keeps a qty-0 row (no crash: the output says orders=4 instead of 3). `main.py` must stay unchanged. The
verifier checks each function's contract with its own data, so hiding the crash in `main.py`, hard-coding the output,
or casting the price in `calc.py` instead of `ingest.py` all FAIL.

`pipeline_omni_v2`: the SAME files, verifier and selftests (same verifier hash), only the prompt differs: the first
reply must be `CMD: run_python main.py`, then one file per fix followed by a run, and the price rule says "convert,
never drop a row because of its price". Made after `pipeline_omni` scored 0/10 on Termux (update 22): compare the
two to measure the prompt alone. Keep `pipeline_omni` unchanged as the baseline.

**Expected-output gate** (update 25): a task may ship `expected_output.txt` (the exact lines the program must print).
The bench passes it to the tool as `AGENT_EXPECT_FILE`, and the tool then refuses DONE until the latest real run
printed those lines (the tool's own run of `main.py` counts), naming the first wrong line. Only `pipeline_omni_v2`
has one; `pipeline_omni` and `bugfix` stay without it as baselines. The report says when the gate is ON and counts
how often it refused a DONE on wrong output, so a PASS that needed the gate is visible. A run with the gate is not
comparable to one without it: it measures "reaches the known target", not "knows on its own when it is done".
