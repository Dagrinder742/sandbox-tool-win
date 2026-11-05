# sandbox-tool-win

**Repository:** https://github.com/Dagrinder742/sandbox-tool-win

The Windows line of sandbox-tool: it runs natively in PowerShell, with `run_python` (and so the DONE gate and the
expected-output gate) limited by Windows Job Objects. It also still runs on Linux, Termux and WSL. The handover log
for this line is `info_win.txt`.

Let a small local model (or Gemini) work in **one folder** on your machine: read and edit files,
write Python, run it and fix what breaks, without giving it a shell.

It was built to make a 3B-parameter model (Qwen2.5-Coder-3B on a phone) usable as a coding agent, so
many of its rules exist because a small model did something silly without them. It is a single Python file with
no dependencies beyond the standard library.

> **Read "What it does not protect against" before pointing this at anything you care about.**
> It is a guard against a model's mistakes and tricks, not a container.

## Genesis & design philosophy

I've been studying Python and software architecture for about a year, and I've worked on this
project for about two months. I can't write the boilerplate myself, so this tool was built in
partnership with AI: Claude (Anthropic) and Gemini (Google) wrote most of the code under my
direction, and a small local model, Qwen2.5-Coder-3B running on my phone through Termux, was
both the test subject and the reason the tool exists. My part was the design goals, the
testing on real hardware, reading the failures, and deciding what the tool should and should
not allow. I'm saying this plainly because it's true, and because I'd want to know it if I
were reading this repo.

The idea: small local models need more than a prompt. They need more capability and tighter
boundaries. A 3B model will invent tool output, say DONE too early, and break a file with a bad
patch. Many of the rules, static checks and verification gates here exist because that
happened in a real run. The `bench/` kit measures how well it works and where the model still
fails; those failures are listed, not hidden.

Docker isn't practically available on stock Android/Termux, so this is a lightweight harness
instead of a container. It reduces risk and makes the model's behaviour observable and logged.

**It is not a security sandbox and does not guarantee safety.** It has known gaps, macOS and
Windows are unverified or unsupported, and it should not be used for untrusted models or
sensitive data without a container or VM around it. Read [SECURITY.md](SECURITY.md) and
"What it does not protect against" below before you run it.

## What the agent can do

Every reply from the model is an optional one-line `NOTE:` (its memory) plus exactly one `CMD:` or a `DONE:`.

| Command | What it does |
|---|---|
| `cat` `ls` `wc` `head` `tail` `grep` `sed` | Allow-listed, flag by flag. No pipes, redirects, chaining or shell. |
| `read_file <path> [start] [count]` | Numbered lines, with a hint to continue reading. |
| `write_file <path>` | Whole file in a fenced block after the command line. Written exactly as sent (no `\n` mangling). |
| `patch_file <path>` | `SEARCH` / `REPLACE` block. Refuses if SEARCH is missing, not unique, equals REPLACE, or would leave a `.py` file with a syntax error. Repairs indentation. |
| `replace_def <path.py> <name>` | Replaces ONE whole function, class or `Class.method` with the complete new definition from a fenced block. The old version is found with `ast`, so the model copies no old text and counts no lines. Refuses an unknown or duplicated name, a block that is not exactly one def/class with the same name, and any result with a syntax error. Space-indented code cannot be spliced into a tab-indented file (it is refused). |
| `check_file <path.py>` | Static check (syntax, stub bodies, shadowed methods, wrong argument counts, stdlib module used but never imported). Never runs the code. Runs automatically after every write/patch of a `.py` file. |
| `run_python <path.py>` | Runs a script under a runtime guard (below). POSIX only. |

Things the tool does so a small model keeps on track:

* **DONE gate.** `DONE` is refused while a `.py` file was changed and not run since, while its last run
  failed, or while a write/patch was refused and never redone. (`exit 0` proves the script ran, not that it
  is correct: check results yourself, for example with the benchmark kit.)
* Repeated identical commands are refused; three refusals/errors in a row stop the run; 10 steps maximum.
* History is trimmed so a long run keeps fitting in a small context window; `NOTE:` lines survive the trimming.
* Refused commands are logged to `<sandbox>.refused.log`, next to the sandbox, and that log is protected.

## Install

Python 3.10 or newer.

```bash
git clone https://github.com/Dagrinder742/sandbox-tool-win.git && cd sandbox-tool-win
pip install .                 # or just run:  python src/sandbox_tool.py ...
pip install ".[gemini]"       # only if you want BACKEND=gemini
```

## Quick start (local model)

1. Run any OpenAI-compatible chat server. With llama.cpp: `llama-server -m your-model.gguf --port 8080`.
2. Choose the folder the agent may touch (it must already exist; if you set nothing, `./sandbox` is created):

   ```bash
   mkdir -p ~/agent-sandbox
   export SANDBOX_DIR=~/agent-sandbox
   ```
3. Check the server, then give the agent a task:

   ```bash
   sandbox-tool --ping
   sandbox-tool --agent "Create hello.py that prints hello, run it, and tell me the output."
   sandbox-tool "ls -la"         # run one allow-listed command by hand
   ```

`--ping` sends a tiny request and prints how fast the server answered. Every agent step also prints a
`[llm]` line with prompt and generation speed, so you can see whether the server is alive and how slow it is.

Use another server (Ollama, LM Studio, vLLM...) with `LLAMA_URL=http://host:port/v1/chat/completions`
and, if it needs the real model name, `LLAMA_MODEL=name`.

### Gemini

```bash
pip install ".[gemini]"
export BACKEND=gemini GEMINI_API_KEY=...   # never put the key in a file you commit
export GEMINI_MODEL=<a model your key can use>
```

Status: the Gemini path is covered by unit tests against a fake SDK. The author has not re-verified this
version against the live API.

## Safety model

**Enforced** (each item has tests in `tests/test_sandbox_tool.py`):

* The sandbox is one folder. Allow-listed commands and the file tools resolve every path with `realpath` and
  refuse anything outside it, including `..` tricks and symlinks that point out.
* `run_python` runs the script in a child process with an audit hook that watches what the script *does*:
  reads, writes, deletes, listings, renames, chmod/links, starting programs, network and `ctypes` outside the
  sandbox are blocked at run time. Obfuscation (base64, `getattr`, `e = exec`) does not help a script because the
  hook looks at actions, not code. A static pre-scan only gives friendlier messages.
* Limits: 10 s wall clock (`RUN_TIMEOUT_S`), CPU and file-size limits, and a memory watchdog (`RUN_MEM_MB`,
  Linux `/proc`). The child gets a clean environment: your API keys are not passed to it.
* This tool's own file and the refusal log are in a `PROTECTED` set that no tool or script can modify.
* The sandbox folder may not be `/` or your home folder (or a parent of it).

**What it does not protect against**

* It is not a container or VM. A determined attacker using interpreter internals could get out. Known gaps:
  `os.mkfifo`, `os.mknod` and `os.chroot` raise no audit event.
* `run_python` needs POSIX limits and is switched off on Windows. macOS is untested.
* The model server you configure can see everything the agent sees. The agent's code has no network, but the
  tool itself calls your model endpoint.
* Anything the model writes *inside the sandbox* is the model's output: read it before you run it yourself.

For untrusted models or anything sensitive, run the whole thing inside a container or VM as well.
Report escapes privately (see `SECURITY.md`).

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_DIR` | `./sandbox` (created) | The folder the agent may use. |
| `BACKEND` | `local` | `local` or `gemini`. |
| `LLAMA_URL` | `http://localhost:8080/v1/chat/completions` | OpenAI-compatible endpoint. |
| `LLAMA_MODEL` | `local-model` | Model name sent in the request. |
| `LLAMA_TIMEOUT` | `900` | Longest silence (s) from the server before giving up; prompt reading counts. |
| `LLAMA_TOTAL_TIMEOUT` | `1800` | Cap (s) for one whole reply. |
| `LLAMA_STREAM` | `1` | `0` sends a plain (non-streaming) request. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | (none), `gemini-3.8-flash` | For `BACKEND=gemini`. Set the model yourself. |
| `RUN_PYTHON` | `1` | `0` switches `run_python` off. |
| `RUN_TIMEOUT_S`, `RUN_MEM_MB` | `10`, `1024` | `run_python` limits (`RUN_MEM_MB=0` = no memory limit). |
| `REQUIRE_VERIFY` | `1` | `0` turns the DONE gate off. |
| `REQUIRE_RUN_BEFORE_PATCH` | `0` | `1`: a `.py` file must be run before it may be patched (strict mode for small models). |
| `REQUIRE_READ_BEFORE_PATCH` | `1` | `0` allows `patch_file` / `replace_def` on a file the model has not looked at this run. |
| `REQUIRE_WORK_BEFORE_DONE` | `1` | `0` allows `DONE` as the very first reply (before any command ran). |
| `MAX_BATCH` | `3` | How many `CMD:` lines from ONE model reply are run, in order. The batch stops at the first refusal or error and after a `run_python`; the rest are reported to the model as NOT RUN. A `DONE` in a reply that ran commands is ignored. `1` = one command per reply (the old behaviour). |
| `PREVIEW_LINES` | `15` | Lines of tool output echoed to your terminal (`0` = all). The model always gets the full output. |
| `AUDIT_LOG` | `<sandbox>.refused.log` | Where refused commands are logged. |
| `AGENT_MAX_STEPS` | `10` | Steps per run. |
| `AGENT_TRANSCRIPT` | (unset) | Save the whole conversation as JSON to this file. |

## Platforms

| Where | State |
|---|---|
| Linux, Termux (Android) | Developed and tested. Everything works. |
| WSL (Ubuntu) | A normal Linux userland, so it should behave like Linux. Run `--selfcheck` to confirm on your machine. |
| Windows PowerShell with Windows Python | `--selfcheck` on Windows 11 / Python 3.14 passed text encoding, CRLF handling and the sandbox boundary (including `..\\`, absolute and symlink paths) and found Git for Windows' tools in `usr\\bin`. It also found that programs could not be started; that is fixed (the tool now starts them by full path) but **not yet re-checked on Windows**. `run_python` is switched off by design (its CPU and memory limits need POSIX), so use WSL or Termux for it. The test suites have not been run on Windows. |
| Git Bash with Windows Python | **Untested**, expected to behave like PowerShell. |
| macOS | Untested. BSD `sed` has no `--sandbox` option, so `sed` is expected to fail there; the other tools should work. |

Run `python src/sandbox_tool.py --selfcheck` (or `sandbox-tool --selfcheck`) on a machine to see what works on **that**
machine: text encoding, line endings, the sandbox boundary, which programs exist, and `run_python`. It runs in a
throwaway folder and prints `ok / FAIL / warn / skip` per line. It checks the tool, not the model.

Text files: the tool reads and writes UTF-8 with the same result on every platform. `write_file` writes LF line
endings. `patch_file` and `replace_def` keep a file's existing line endings (a CRLF file stays CRLF).

## Termux / Android

Developed and used on Termux. Set `SANDBOX_DIR` explicitly to a dedicated folder under your home folder.
Child processes find `cat`, `grep` and `sed` through `$PREFIX/bin` automatically. On a phone, prompt reading is
the slow part (the first agent step reads the whole system prompt); later steps reuse the server's cache.
Things that helped llama-server there: `-t 4 -np 1` and keeping the phone awake.

## Tests

```bash
python tests/test_sandbox_tool.py   # the guard itself; must print ALL OK
python bench/test_bench.py          # the benchmark kit, against a fake model server; must print ALL OK
pytest                              # runs both
```

Any change to `src/sandbox_tool.py` must keep the first suite green; a change that makes it fail has weakened the guard.
Where `run_python` is off (Windows by design, or `RUN_PYTHON=0`) the 61 checks that need it are skipped and counted, listed by name in `tests/needs_run_python.txt`; every other check still has to pass. With `run_python` on, nothing is skipped.
Developed and tested on Linux (Python 3.11 to 3.13) and Termux. The included GitHub Actions workflow lists Python 3.10 to 3.13 and macOS, but it has not run yet, so those are untested.

## Benchmark kit

`bench/` is a small task bank with verifiers that judge the **file the model wrote**, not what the model
says. It records every run, writes one report, lets you add notes, compares sessions and can export verified
runs. See [`bench/README.md`](bench/README.md).

Informal results from the author's own runs (one model, a handful of tasks, 10 runs per batch, so read these
as hints, not measurements): the same 3B model on one coding task passed 7/10 runs, then 10/10 after one line
was added to the system prompt and the tool started flagging a forgotten `import`. A harder rewrite of the
task dropped to 0/10 and came back to 9/10 once the task text spelled out its logic.

## License

MIT. See `LICENSE`.
