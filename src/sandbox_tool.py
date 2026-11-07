#!/usr/bin/env python3
"""
sandbox_tool.py - lets a local GGUF model run a small allowlist of terminal
commands and secure file modification tools inside one sandbox directory.

SECURITY CONTRACT (what is enforced, and where):
1. SANDBOX CONTAINMENT. Allowlisted commands and the built-ins (read_file, write_file,
   patch_file, check_file) resolve every path with realpath and refuse anything outside
   SANDBOX. run_python enforces the same thing at RUNTIME with an audit hook inside the
   child process (reads, writes, deletes, listings, renames, chmod, links, subprocesses,
   network, ctypes). A script cannot reach outside no matter how it is written, because
   the hook watches what it DOES, not what it looks like. Obfuscation (base64, getattr,
   aliases like e = exec) does not get around it.
2. FILE PROTECTION. This script and the refused-commands log are in PROTECTED: no tool and no
   run_python script may modify them. chmod 444 / attrib +r is a second layer only; it
   does NOT stop a rename-over or a chmod-back, so PROTECTED is the real defence.
3. AUTHORIZED OVERRIDES. A human architect (or an external agent working for one) edits
   this file with the permission bit toggled (chmod u+w / attrib -r), then re-locks it.
   After ANY edit, run:  python tests/test_sandbox_tool.py   It must print ALL OK. A change
   that makes it fail has weakened the guard; do not ship it.
4. EXECUTION SAFETY. Only run_python runs code. It is POSIX-only (Linux, Termux, WSL).
   On Windows it is switched off, because the limits it relies on do not exist there.
   There is no shell: pipes, redirects, chaining, python -c and base64 tricks do nothing.
5. STATE VERIFICATION. The agent loop refuses DONE while a .py file was changed and not
   run since, while its last run failed, or while a write/patch was refused and never
   redone (REQUIRE_VERIFY=0 turns this off). exit 0 proves it ran, not that it is right.
KNOWN GAPS: os.mkfifo / os.mknod / os.chroot have no audit event; a determined attacker
using interpreter internals could still get out. This is a guard against a model's
mistakes and tricks, not a container.
"""
import os
import sys

# Defense in depth: when run as a script, don't let a file sitting next to it
# (e.g. a model-written json.py) shadow the stdlib modules imported below.
if (__name__ == "__main__" and sys.path
        and os.path.realpath(sys.path[0] or ".") == os.path.dirname(os.path.realpath(__file__))):
    sys.path.pop(0)

import ast
import builtins
import glob
import io
import hashlib
import json
import re
import tokenize
import shlex
import shutil
import signal
import site
import subprocess
import sysconfig
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

__version__ = "0.1.0+win.10.1"


def _default_sandbox() -> Path:
    """SANDBOX_DIR if set, otherwise ./sandbox under the folder you run the tool from.

    Never the folder this file lives in: if the package is installed that would be
    site-packages. The folder is created by main() when it is the default."""
    env = os.environ.get("SANDBOX_DIR")
    return (Path(env).expanduser() if env else Path.cwd() / "sandbox").resolve()


SANDBOX = _default_sandbox()


def _safe_path() -> str:
    """PATH for child processes: standard system folders plus the folder of the running
    Python (on Termux that is $PREFIX/bin, where cat/grep/sed live). Nothing machine-specific
    is written into this file. Only folders that exist are listed.

    No "which operating system?" check: every candidate is listed and kept only if it exists.
    Git for Windows' usr\\bin (where cat, ls, grep, sed, head, tail and wc live on Windows) is a
    candidate only when ProgramFiles / LOCALAPPDATA name it; those variables are not set on
    Linux, Termux or WSL, so there it never appears. The POSIX folders come first, so a POSIX
    machine always prefers its own programs. Verified on real Windows in updates 19-20."""
    parts = [os.path.join(os.environ["PREFIX"], "bin")] if os.environ.get("PREFIX") else []
    parts += [os.path.dirname(os.path.realpath(sys.executable)), "/usr/local/bin", "/usr/bin", "/bin"]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            parts.append(os.path.join(base, "Git", "usr", "bin"))
    if os.environ.get("LOCALAPPDATA"):
        parts.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Git", "usr", "bin"))
    return os.pathsep.join(p for p in dict.fromkeys(parts) if os.path.isdir(p))


SAFE_PATH = _safe_path()


def _find_exe(name: str):
    """Full path of an allowlisted program, found ONLY in SAFE_PATH, or None.

    Why a full path: on Windows, subprocess looks a bare name like "cat" up with the PARENT's PATH, not the
    PATH given for the child (real Windows selfcheck: cat.EXE was found but would not start). Why the
    checks: Windows also searches the current folder first, so a program (cat.bat) that the model wrote into
    the sandbox must never be picked up. A result outside SAFE_PATH, or inside the sandbox, is refused."""
    exe = shutil.which(name, path=SAFE_PATH)
    if not exe:
        return None
    exe = os.path.abspath(exe)
    allowed = {os.path.normcase(os.path.realpath(d)) for d in SAFE_PATH.split(os.pathsep) if d}
    here = os.path.normcase(os.path.realpath(os.path.dirname(exe)))
    real = Path(os.path.realpath(exe))
    if here not in allowed or real == SANDBOX or SANDBOX in real.parents:
        return None
    return exe


_CHILD_PASSTHROUGH = ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT")


def _child_env() -> dict:
    """The only environment a child command gets: PATH, HOME, LC_ALL, plus the few variables in
    _CHILD_PASSTHROUGH if (and only if) the parent has them. Windows cannot start a program (or
    load its DLLs) without SystemRoot; Linux/Termux do not set these, so there the child env stays
    exactly PATH, HOME, LC_ALL. No other variable (tokens, keys) ever passes through."""
    env = {"PATH": SAFE_PATH, "HOME": str(SANDBOX), "LC_ALL": "C.UTF-8"}
    for k in _CHILD_PASSTHROUGH:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _has_posix_limits() -> bool:
    """True if this Python has what run_python's limits need: the resource module (CPU and file-size
    limits), _posixsubprocess (blocked inside the child) and os.killpg/os.setsid (kill the whole
    process group on timeout). Asks for the capability instead of the OS name: Windows has none of
    these, so run_python stays off there exactly as before."""
    try:
        import resource, _posixsubprocess  # noqa: F401
    except ImportError:
        return False
    return hasattr(os, "killpg") and hasattr(os, "setsid")


def _has_job_objects() -> bool:
    """True if this Python can put run_python's child into a Windows Job Object (memory cap, one-process limit,
    kill-on-close, CPU backstop). Asks for the capability, not the OS name. Verified on Casey's Windows 11 PC by
    win_probe.py (W1): memory cap, process block and kill-on-close work; the CPU cap fires but late (~9 s for a
    2 s cap), so the wall-clock deadline stays the real timer."""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        return all(hasattr(k32, f) for f in ("CreateJobObjectW", "AssignProcessToJobObject",
                                             "SetInformationJobObject", "TerminateJobObject"))
    except (ImportError, AttributeError, OSError):
        return False
TIMEOUT_S = 300
MAX_OUTPUT = 8000
MAX_GLOB = 200
ALLOW_SED_INPLACE = True
PREVIEW_LINES = int(os.environ.get("PREVIEW_LINES", "15"))  # 0 = show everything
# Files the model may read but never modify (this script, if it lives in the sandbox).
# Refusals are logged next to (not inside) the sandbox, e.g. ./sandbox.refused.log
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", str(SANDBOX.parent / f"{SANDBOX.name}.refused.log"))).resolve()
PROTECTED = {Path(__file__).resolve(), AUDIT_LOG.resolve()}
# W10: files the model may read and RUN but never change (a task's test file, while an agent run uses it).
READ_ONLY = set()
# run_python limits (see the notes above cmd_run_python)
RUN_PYTHON = os.environ.get("RUN_PYTHON", "1") != "0" and (_has_posix_limits() or _has_job_objects())  # off where no limits work
RUN_TIMEOUT_S = int(os.environ.get("RUN_TIMEOUT_S", "10"))
REQUIRE_VERIFY = os.environ.get("REQUIRE_VERIFY", "1") != "0"                 # block DONE until code was run
REQUIRE_READ_BEFORE_PATCH = os.environ.get("REQUIRE_READ_BEFORE_PATCH", "1") == "1"  # patching a file you never looked at is a guess
REQUIRE_WORK_BEFORE_DONE = os.environ.get("REQUIRE_WORK_BEFORE_DONE", "1") == "1"  # DONE as the very first reply does nothing
REQUIRE_RUN_BEFORE_PATCH = os.environ.get("REQUIRE_RUN_BEFORE_PATCH", "0") == "1"  # strict mode for small models
RUN_MEM_MB = int(os.environ.get("RUN_MEM_MB", "1024"))  # watched from outside; 0 = no limit
RUN_MAX_FILE_MB = 20
RUN_MAX_WRITE_MB = 2 * RUN_MAX_FILE_MB   # Windows job path: TOTAL bytes the script may write (files + output); no per-file limit exists there

_F = "flag"
_V = "value"
_P = "pattern"
_I = "inplace"

_HEADTAIL = {"lines": _V, "bytes": _V, "quiet": _F, "silent": _F, "verbose": _F}

POLICY = {
    "cat": dict(pattern=False, short_val="", pattern_short="", short_deny="",
                long={k: _F for k in (
                    "number", "number-nonblank", "show-all", "show-ends",
                    "show-tabs", "squeeze-blank", "show-nonprinting")}),
    "ls": dict(pattern=False, short_val="", pattern_short="", short_deny="",
               long={k: _F for k in (
                   "all", "almost-all", "human-readable", "recursive", "reverse",
                   "size", "classify", "directory", "color",
                   "group-directories-first")}),
    "wc": dict(pattern=False, short_val="", pattern_short="", short_deny="",
               long={k: _F for k in (
                   "lines", "words", "bytes", "chars", "max-line-length")}),
    "head": dict(pattern=False, short_val="nc", pattern_short="", short_deny="",
                 long=dict(_HEADTAIL)),
    "tail": dict(pattern=False, short_val="nc", pattern_short="", short_deny="fF",
                 long=dict(_HEADTAIL)),
    "grep": dict(pattern=True, short_val="ABCmdDeX", pattern_short="e",
                 short_deny="fR",
                 long={
                     "regexp": _P, "max-count": _V, "after-context": _V,
                     "before-context": _V, "context": _V, "binary-files": _V,
                     "devices": _V, "directories": _V, "include": _V,
                     "exclude": _V, "exclude-dir": _V,
                     **{k: _F for k in (
                         "line-number", "recursive", "ignore-case", "invert-match",
                         "count", "files-with-matches", "files-without-match",
                         "only-matching", "word-regexp", "line-regexp",
                         "fixed-strings", "extended-regexp", "perl-regexp",
                         "basic-regexp", "no-filename", "with-filename", "quiet",
                         "no-messages", "text", "color", "colour")}}),
    "sed": dict(pattern=True, short_val="el", pattern_short="e", short_deny="f",
                long={"expression": _P, "line-length": _V, "in-place": _I,
                      **{k: _F for k in (
                          "quiet", "silent", "regexp-extended", "separate",
                          "null-data", "posix", "unbuffered")}}),
}
SHELL_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1"}


class Refused(Exception):
    pass


def _read_text(path, errors="strict") -> str:
    """Read a text file as UTF-8 with line endings turned into \\n, the same on Windows, Linux and Termux.
    (Path.read_text() uses the machine's locale: cp1252 on a Windows console, which garbles UTF-8.)"""
    with open(path, "r", encoding="utf-8", errors=errors, newline=None) as f:
        return f.read()


def _uses_crlf(path) -> bool:
    """True if the file on disk has Windows line endings, so an edit can keep them."""
    try:
        return b"\r\n" in Path(path).read_bytes()
    except OSError:
        return False


def _write_text(path, text: str, crlf: bool = False) -> None:
    """Write UTF-8 with an explicit line ending: \\n unless the file already used \\r\\n. (Path.write_text()
    would turn every \\n into \\r\\n on Windows and write the locale's encoding.)"""
    with open(path, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)


def _inside(p: str) -> bool:
    real = Path(os.path.realpath(SANDBOX / p))
    return real == SANDBOX or SANDBOX in real.parents


def _check_path(p: str):
    if not _inside(p):
        raise Refused(f"path outside sandbox: {p}")


def _check_writable(p: str):
    _check_path(p)
    real = Path(os.path.realpath(SANDBOX / p))
    if real in PROTECTED:
        raise Refused(f"protected file, cannot modify: {p}")
    if real in READ_ONLY:
        raise Refused(f"{p} is the task's test file: it checks what correct code does, so it cannot be changed. "
                      f"Fix the code it tests, then run_python {p} again")
    if "__pycache__" in real.parts:  # never let the model plant bytecode
        raise Refused(f"cannot write inside __pycache__: {p}")


def _fenced_block(body):
    """Text of the ``` block in `body`, or None if there is no block.

    The block ends at the first closing fence that is followed by nothing, or by a
    protocol line (NOTE:/CMD:/DONE:). That keeps file content free to contain its own
    fences (markdown), but stops at the model's real closing fence when the model keeps
    writing and invents the next steps (fake tool output, more CMD lines) after it.
    If no closing fence qualifies, the LAST bare fence ends the block (older behaviour).
    An unclosed block means the reply was probably cut off by the token limit; refuse
    rather than write a truncated file."""
    lines = (body or "").splitlines()
    bounds = _fence_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    return "\n".join(lines[start + 1:end]) + "\n"


def _fence_bounds(lines):
    """(index of the opening fence, index of the closing fence) of the first ``` block in
    `lines`, or None if there is none. Raises Refused for an unclosed block. See _fenced_block."""
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("```")), None)
    if start is None:
        return None
    closers = [i for i in range(start + 1, len(lines)) if lines[i].strip() == "```"]
    if not closers:
        raise Refused("code block is not closed (reply may have been cut off); "
                      "write a shorter file or split it into parts")

    def _ends_reply(i):
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
        return not nxt or nxt.startswith(("NOTE:", "CMD:", "DONE:"))
    return start, next((i for i in closers if _ends_reply(i)), closers[-1])


def _search_replace(body):
    """(search, replace) from a SEARCH/REPLACE block in `body`, or None if absent."""
    lines = (body or "").splitlines()
    top = next((i for i, l in enumerate(lines) if l.strip().startswith("<<<<<<<")), None)
    if top is None:
        return None
    mid = next((i for i in range(top + 1, len(lines)) if lines[i].rstrip() == "======="), None)
    end = None
    if mid is not None:
        end = next((i for i in range(mid + 1, len(lines)) if lines[i].strip().startswith(">>>>>>>")), None)
    if mid is None or end is None:
        raise Refused("malformed SEARCH/REPLACE block; expected <<<<<<< SEARCH, =======, >>>>>>> REPLACE")
    if any(l.strip().startswith("<<<<<<<") for l in lines[end + 1:]):
        raise Refused("two or more SEARCH/REPLACE blocks in one reply. Nothing was changed. "
                      "Send ONE patch_file per reply (one CMD line and one block), wait for the "
                      "result, then send the next edit")
    return "\n".join(lines[top + 1:mid]), "\n".join(lines[mid + 1:end])


_DEF_LINE_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def _replace_def_hint(path_str: str, search_text: str) -> str:
    """When a model writes patch_file with SEARCH == REPLACE == one 'def name(...):' line (and the new
    function somewhere else), it wants replace_def. Return a ready command naming the function, or ''."""
    lines = [l for l in search_text.splitlines() if l.strip()]
    m = _DEF_LINE_RE.match(lines[0]) if len(lines) == 1 else None
    if not m or not path_str.endswith(".py"):
        return ""
    try:
        tree = ast.parse(_read_text(SANDBOX / path_str))
    except (OSError, SyntaxError, ValueError):
        return ""
    found = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if ch.name == m.group(1):
                    found.append(prefix + ch.name)
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)
    walk(tree, "")
    if len(found) != 1:
        return ""
    return (f"\nTo replace a WHOLE function, use replace_def: the old one is removed for you and there is no "
            f"SEARCH text to copy:\nCMD: replace_def {path_str} {found[0]}\n```python\n"
            f"<the complete new {m.group(1)} goes here>\n```")


def _text_after_block(body):
    """Lines that follow the closing >>>>>>> line, ignoring blanks, a closing fence and
    NOTE:/CMD:/DONE: lines. Small models put the new code there instead of inside REPLACE."""
    lines = (body or "").splitlines()
    end = next((i for i, l in enumerate(lines) if l.strip().startswith(">>>>>>>")), None)
    if end is None:
        return []
    return [l for l in lines[end + 1:]
            if l.strip() and l.strip() != "```" and not l.strip().startswith(("NOTE:", "CMD:", "DONE:"))]


MAX_CHECK_BYTES = 200_000
_PLACEHOLDER_RE = re.compile(r"\b(todo|fixme|placeholder)\b|implement\b.*\bhere\b", re.I)


def _is_stub(fn) -> bool:
    """True if a function body is only pass / ... / a docstring / raise NotImplementedError."""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        return True  # docstring only
    if len(body) != 1:
        return False
    s = body[0]
    if isinstance(s, ast.Pass):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
        return True
    if isinstance(s, ast.Raise) and s.exc is not None:
        e = s.exc.func if isinstance(s.exc, ast.Call) else s.exc
        return isinstance(e, ast.Name) and e.id == "NotImplementedError"
    return False


def _arity(fn):
    """(min, max) positional args a method accepts after self; max None = unlimited."""
    a = fn.args
    pos = a.posonlyargs + a.args
    n = max(len(pos) - 1, 0)  # drop self
    required = max(len(pos) - len(a.defaults) - 1, 0)
    return required, (None if a.vararg else n)


_STDLIB = getattr(sys, "stdlib_module_names", frozenset(
    "abc argparse ast asyncio base64 collections copy csv datetime decimal enum functools "
    "glob hashlib heapq io itertools json logging math operator os pathlib pickle queue random "
    "re shutil sqlite3 statistics string struct subprocess sys tempfile textwrap threading time "
    "typing unittest uuid warnings".split()))


def _missing_imports(tree, source: str) -> list:
    """(line, message) for stdlib modules used as `name.attr` that are never bound in the file."""
    bound = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
    first = next((l.rstrip() for l in source.splitlines() if l.strip()), "")
    firstuse = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in _STDLIB and not n.value.id.startswith("_")
                and n.value.id not in bound):
            firstuse[n.value.id] = min(firstuse.get(n.value.id, n.lineno), n.lineno)
    out = []
    for name, line in sorted(firstuse.items(), key=lambda kv: kv[1]):
        fix = (f"Add `import {name}` as the very FIRST line of the file (line 1), above everything else, "
               f"never inside a class, function or dict.")
        if first and not first.startswith((" ", "\t")):
            fix += (f" With patch_file: SEARCH = `{first[:70]}`, REPLACE = `import {name}`, "
                    f"an empty line, then `{first[:70]}`.")
        out.append((line, f"`{name}` is used but never imported, so this will fail with NameError. {fix}"))
    return out


def check_python(source: str) -> list[str]:
    """Static check only: the code is parsed, never executed. Returns problem lines.

    Catches syntax errors, stub bodies, placeholder comments, methods shadowed by
    self.<name> assignments, wrong argument counts on self.method() calls, and demo
    code that runs on import. It does NOT prove the program works."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        text = (e.text or "").strip()
        msg = e.msg
        if isinstance(e, IndentationError) or "indent" in msg.lower():
            msg += ". Every line of a REPLACE block needs the file's exact leading spaces (4 per level)."
        return [f"SYNTAX ERROR line {e.lineno}: {msg}" + (f"  -> {text}" if text else "")]

    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_stub(node):
            problems.append((node.lineno, f"def {node.name}() has no real logic (only pass/.../docstring). Write the actual code."))

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and _PLACEHOLDER_RE.search(tok.string):
                problems.append((tok.start[0], f"placeholder comment: {tok.string.strip()[:60]}"))
    except (tokenize.TokenError, IndentationError):
        pass

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        methods = {m.name: m for m in cls.body
                   if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.decorator_list}
        for m in (m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if m.decorator_list or not m.args.args:
                continue
            me = m.args.args[0].arg
            for n in ast.walk(m):
                targets = []
                if isinstance(n, ast.Assign):
                    targets = n.targets
                elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                    targets = [n.target]
                for t in targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == me and t.attr in methods):
                        problems.append((n.lineno, f"{me}.{t.attr} is assigned, but {t.attr}() is also a method of class "
                                         f"{cls.name}. This replaces the method, so calling {me}.{t.attr}() will fail. Use another name."))
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == me
                        and n.func.attr in methods
                        and not n.keywords and not any(isinstance(x, ast.Starred) for x in n.args)):
                    lo, hi = _arity(methods[n.func.attr])
                    got = len(n.args)
                    if got < lo or (hi is not None and got > hi):
                        want = f"{lo}" if lo == hi else f"{lo}-{hi if hi is not None else 'many'}"
                        problems.append((n.lineno, f"{me}.{n.func.attr}() called with {got} argument(s) but "
                                         f"def {n.func.attr} (line {methods[n.func.attr].lineno}) takes {want}."))

    # A standard-library module used as name.attr but never imported or assigned anywhere
    # (the classic NameError a small model forgets: random.randint with no `import random`).
    problems += _missing_imports(tree, source)

    # Module-like files (they define something) should not run demo calls on import.
    # A plain script with no defs, like print("hi"), is left alone.
    module_like = any(isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for n in tree.body)
    for stmt in tree.body if module_like else []:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            problems.append((stmt.lineno, "this call runs when the file is imported. Put demo code under "
                             "if __name__ == \"__main__\":"))

    problems.sort(key=lambda p: p[0])
    seen, out = set(), []
    for ln, msg in problems:
        if (ln, msg) not in seen:
            seen.add((ln, msg))
            out.append(f"line {ln}: {msg}")
    return out


def format_check(problems: list[str]) -> str:
    if not problems:
        return ("[check] no problems found by the static check. "
                "The code was NOT executed, so this does not prove it runs correctly.")
    return (f"[check] {len(problems)} problem(s) found. Fix them with patch_file:\n"
            + "\n".join("  " + p for p in problems[:15]))


def cmd_check_file(path_str: str) -> str:
    """Static check of a .py file inside the sandbox. Never executes it."""
    try:
        _check_path(path_str)
        target = SANDBOX / path_str
        if target.suffix != ".py":
            return "REFUSED: check_file only checks .py files"
        if not target.is_file():
            return f"ERROR: no such file: {path_str}"
        if target.stat().st_size > MAX_CHECK_BYTES:
            return f"REFUSED: file larger than {MAX_CHECK_BYTES} bytes"
        return "[exit 0]\n" + format_check(check_python(_read_text(target, "replace")))
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _with_check(result: str, path_str: str) -> str:
    """Append the static check to a successful write/patch of a .py file."""
    if not result.startswith("[exit 0]") or not path_str.endswith(".py"):
        return result
    return result + "\n" + cmd_check_file(path_str).removeprefix("[exit 0]\n")


# ------------------------------------------------------------ run_python (Phase 2)
# Runs a model-written .py file in a child process. READ THIS FIRST:
#  * The real enforcement is the runtime audit hook in _RUNNER. It sees the actual
#    operation (open, remove, subprocess, connect...), so obfuscation such as
#    getattr(__import__("o"+"s"), "sys"+"tem") does not get past it.
#  * scan_python() is only a friendly pre-flight: it refuses obvious calls before
#    anything runs, so the model gets a clear message. It is NOT a security check.
#  * This guards against a literal-minded 3B model making accidents. It is not a
#    sandbox against someone deliberately attacking it (no container exists on Termux).
#  * Known gaps (found by testing, not fixable with an audit hook): os.mkfifo, os.mknod,
#    os.chroot and os.sethostname raise no audit event, so a script can create an empty
#    FIFO outside the sandbox. Anyone willing to dig into interpreter internals can also
#    get around it. Deleting is allowed only in the run's own temp folder.
#  * Memory is limited by watching the child's resident size from /proc (RUN_MEM_MB),
#    not with RLIMIT_AS: on Android that made Python abort at startup (found on Termux).
#  * Reads are limited to the sandbox plus Python's own library folders. (An earlier
#    version allowed all of sys.prefix; on Termux that includes usr/tmp, usr/etc and
#    usr/var, and the test suite caught a script reading a file there.)
#  * Verified on Termux: the /proc memory watch and RLIMIT_CPU/RLIMIT_FSIZE work.
#    Still untested there: start_new_session / killpg on timeout. If a normal script fails with "reading outside the sandbox",
#    note the path it names; that folder probably belongs in _read_roots().
_SCAN_CALLS = {
    "os.system", "os.popen", "os.fork", "os.forkpty", "os.kill", "os.killpg",
    "os.chmod", "os.chown", "os.lchown", "os.symlink", "os.link", "shutil.chown", "pty.spawn",
}  # deleting is not listed: the runtime hook decides (it allows the run's own temp folder)
_SCAN_PREFIX = ("subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "ctypes.")
_SCAN_ATTRS = {"chmod", "symlink_to", "hardlink_to"}
_SCAN_IMPORTS = {"subprocess", "ctypes", "pty"}


def scan_python(source: str) -> list[str]:
    """Pre-flight list of clearly blocked calls/imports (line-numbered). [] if none
    or if the file does not parse (the run then reports the SyntaxError itself)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    alias = {}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                if a.name.split(".")[0] in _SCAN_IMPORTS:
                    out.append((n.lineno, f"import {a.name} is blocked"))
        elif isinstance(n, ast.ImportFrom) and n.module:
            if n.module.split(".")[0] in _SCAN_IMPORTS:
                out.append((n.lineno, f"import from {n.module} is blocked"))
            for a in n.names:
                alias[a.asname or a.name] = f"{n.module}.{a.name}"
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        parts, f = [], n.func
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            parts.append(alias.get(f.id, f.id))
            name = ".".join(reversed(parts))
            if name in _SCAN_CALLS or name.startswith(_SCAN_PREFIX):
                out.append((n.lineno, f"{name}() is blocked (other programs, chmod and links are not allowed)"))
                continue
        if isinstance(n.func, ast.Attribute) and n.func.attr in _SCAN_ATTRS:
            out.append((n.lineno, f".{n.func.attr}() is blocked (chmod and links are not allowed)"))
    return [f"line {ln}: {msg}" for ln, msg in sorted(set(out))]


# Child-process program. It installs the audit hook, then runs the target file.
# Everything the hook needs is bound inside _install() so the target script cannot
# swap it out by editing builtins or module globals. It must not use backslashes.
_RUNNER = r'''
import sys, os, json, runpy, traceback
try:
    import resource
except ImportError:          # Windows: the limits come from the Job Object the tool put this process in
    resource = None
cfg = json.loads(sys.argv[1])
target = sys.argv[2]
sys.dont_write_bytecode = True
sys.path.append(cfg["sandbox"])  # appended, so a model-written json.py cannot shadow the stdlib
for _n, _v in (("RLIMIT_CPU", cfg["cpu"]), ("RLIMIT_FSIZE", cfg["fsize"])):
    if _v and resource is not None:
        try:
            resource.setrlimit(getattr(resource, _n), (_v, _v))
        except (ValueError, OSError, AttributeError):
            pass


class SandboxBlocked(PermissionError):
    pass


def _no_exec(*a, **k):
    raise SandboxBlocked("sandbox: starting other programs is not allowed")


# multiprocessing (spawn/forkserver) starts new interpreters through this low-level
# function, which raises no audit event, so replace it before the hook goes in.
import subprocess
try:
    import _posixsubprocess
    _posixsubprocess.fork_exec = _no_exec
    subprocess._fork_exec = _no_exec
except ImportError:          # Windows: programs start through _winapi.CreateProcess (the Job Object also blocks it)
    import _winapi
    _winapi.CreateProcess = _no_exec


def _install(cfg):
    norm = os.path.normcase                  # Windows paths ignore case; on POSIX this changes nothing
    SB = norm(cfg["sandbox"])
    TMP = norm(cfg["tmp"])
    PROT = frozenset(norm(p) for p in cfg["protected"])
    READ = tuple(norm(r) for r in cfg["read_roots"]) + (SB,)
    DEVS = frozenset(norm(d) for d in ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
                                       os.path.realpath(os.devnull)))
    realpath, fsdecode, sep, alt, isabs = os.path.realpath, os.fsdecode, os.sep, os.altsep, os.path.isabs
    O_W = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    PROC = frozenset(("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                      "os.fork", "os.forkpty", "os.kill", "os.killpg", "pty.spawn",
                      "os.startfile", "_winapi.CreateProcess", "_winapi.CreateNamedPipe",
                      "_winapi.OpenProcess", "_winapi.TerminateProcess", "os.add_dll_directory"))
    PERM = frozenset(("os.chmod", "os.chown", "os.lchown", "os.lchmod", "os.chflags",
                      "os.setxattr", "os.removexattr", "os.symlink", "os.link"))
    NET = frozenset(("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                     "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                     "socket.getnameinfo"))
    NATIVE = frozenset(("ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function"))
    GC = frozenset(("gc.get_objects", "gc.get_referrers", "gc.get_referents"))

    def hook(event, args, isinstance=isinstance, any=any, str=str, int=int, bool=bool,
             Blocked=SandboxBlocked):
        def real(p):
            try:
                s = fsdecode(p)
            except Exception:
                return None
            # With dir_fd the kernel resolves a relative path from another folder than
            # the one checked here, and '..' lets that escape. Refuse such paths.
            # (Windows accepts both path separators, so both are split on.)
            if not isabs(s) and ".." in (s.replace(alt, sep) if alt else s).split(sep):
                raise Blocked("sandbox: relative paths containing '..' are not allowed; "
                              "use a path inside the sandbox without '..'")
            try:
                return norm(realpath(s))
            except Exception:
                return None

        def under(p, roots):
            if p is None:
                return False
            for r in roots:
                if p == r or p.startswith(r + sep):
                    return True
            return False

        def write_ok(p):
            return under(p, (SB,)) and p not in PROT and "__pycache__" not in p.split(sep)

        def deny(msg):
            raise Blocked("sandbox: " + msg)

        if event == "import":
            if args[0] == "multiprocessing" or args[0].startswith("multiprocessing."):
                deny("multiprocessing is not allowed (it starts other programs); use threads or plain loops")
        elif event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                return
            p = real(path)
            wr = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                 (isinstance(flags, int) and bool(flags & O_W))
            if wr:
                if not write_ok(p):
                    deny("writing outside the sandbox, or to a protected file, is not allowed: %s" % (path,))
            elif not (under(p, READ) or p in DEVS):
                deny("reading outside the sandbox is not allowed: %s" % (path,))
        elif event in ("os.listdir", "os.scandir"):
            path = args[0]
            if not isinstance(path, int) and not under(real("." if path is None else path), READ):
                deny("listing a folder outside the sandbox is not allowed: %s" % (path,))
        elif event == "os.chdir":
            if not isinstance(args[0], int) and not under(real(args[0]), (SB,)):
                deny("changing to a folder outside the sandbox is not allowed")
        elif event in ("os.mkdir", "os.truncate"):
            if not isinstance(args[0], int) and not write_ok(real(args[0])):
                deny("creating or changing files outside the sandbox, or protected ones, is not allowed")
        elif event == "os.rename":
            for q in args[:2]:
                if not write_ok(real(q)):
                    deny("moving files outside the sandbox, or touching a protected file, is not allowed")
        elif event in ("os.remove", "os.rmdir", "shutil.rmtree"):
            if isinstance(args[0], int) or not under(real(args[0]), (TMP,)):
                deny("deleting files is switched off (only this run's temp folder can be cleaned)")
        elif event in PERM:
            deny("chmod/chown and links are not allowed (%s)" % event)
        elif event in PROC:
            deny("starting or killing other processes is not allowed (%s)" % event)
        elif event in NATIVE:
            deny("ctypes / native code is not allowed")
        elif event in NET:
            deny("network access is not allowed")
        elif event in GC:
            deny("gc introspection is not allowed")
        elif event.startswith("winreg."):
            deny("the Windows registry is not allowed")
        elif event == "_winapi.CreateFile":
            if not write_ok(real(args[0])):
                deny("low-level file access outside the sandbox is not allowed: %s" % (args[0],))
        elif event == "sqlite3.connect":
            db = args[0]
            if isinstance(db, (str, bytes)) and db not in (":memory:", "", b":memory:", b"") \
                    and not write_ok(real(db)):
                deny("databases outside the sandbox are not allowed")

    sys.addaudithook(hook)


_handshake = cfg.get("handshake")
_install(cfg)
del _install, cfg
if _handshake:      # Windows: wait until the tool has put this process into its Job Object (W1 probe design)
    if sys.stdin.readline() != "go" + chr(10):   # anything else (end-of-file: the tool gave up on us) = never run
        sys.exit(126)
del _handshake
try:
    runpy.run_path(target, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    _et, _ev, _tb = sys.exc_info()
    while _tb is not None and _tb.tb_frame.f_code.co_filename != target:
        _tb = _tb.tb_next
    _t = _tb
    while _t is not None and _t.tb_next is not None:  # drop this runner's own hook frames
        if _t.tb_next.tb_frame.f_code.co_filename == "<string>":
            _t.tb_next = None
            break
        _t = _t.tb_next
    traceback.print_exception(_et, _ev, _tb)
    sys.exit(1)
'''


def _read_roots() -> list[str]:
    """Folders a script may READ besides the sandbox: only Python's own code and
    libraries. Never a whole prefix such as /usr or Termux's usr: that also holds tmp/,
    etc/ (ssh host keys, configs) and var/, which a script has no business reading."""
    paths = sysconfig.get_paths()
    roots = {paths.get(k) for k in ("stdlib", "platstdlib", "purelib", "platlib")}
    roots.add(os.path.dirname(os.__file__))
    try:
        roots.update(site.getsitepackages())
    except Exception:
        pass
    roots.add("/usr/share/zoneinfo")
    roots.add(os.path.join(sys.prefix, "share", "zoneinfo"))
    # never the install folder itself: on Windows site.getsitepackages() returns sys.prefix (C:\\Python314) as a
    # "site-packages" entry, which would make the whole Python folder readable (seen on real Windows, update 22)
    whole = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix) if p}
    return sorted({os.path.realpath(r) for r in roots if r and os.path.isabs(r)} - {"/"} - whole)


def _head_tail(f, head=2000, tail=6000) -> str:
    size = f.seek(0, 2)
    f.seek(0)
    if size <= head + tail:
        return f.read().decode("utf-8", "replace")
    first = f.read(head).decode("utf-8", "replace")
    f.seek(size - tail)
    last = f.read(tail).decode("utf-8", "replace")
    return f"{first}\n...[{size - head - tail} bytes of output omitted]...\n{last}"


def _rss_mb(pid):
    """Resident memory of a process in MB from /proc, or None if it cannot be read."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class _WinJob:
    """One Windows Job Object for one run_python child (W2). Every Win32 call of the Windows path is in this class,
    so the rest of that path can be tested on any OS with a stand-in object that has the same five methods.
    Limits: memory cap per process (counts committed memory, swap included), at most ONE process (a script cannot
    start programs), kill everything when the job closes, die on an unhandled crash, and a CPU-time backstop."""
    MB = 1024 * 1024

    def __init__(self, mem_mb: int, cpu_s: float, procs: int = 1):
        import ctypes
        self.ct = ctypes
        D, H, I64 = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int64

        class Basic(ctypes.Structure):          # JOBOBJECT_BASIC_LIMIT_INFORMATION (64 bytes on x64)
            _fields_ = [("PerProcessUserTimeLimit", I64), ("PerJobUserTimeLimit", I64), ("LimitFlags", D),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", D), ("Affinity", ctypes.c_size_t), ("PriorityClass", D),
                        ("SchedulingClass", D)]

        class Io(ctypes.Structure):             # IO_COUNTERS
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Ext(ctypes.Structure):            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on x64)
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Acct(ctypes.Structure):           # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [("TotalUserTime", I64), ("TotalKernelTime", I64), ("ThisPeriodTotalUserTime", I64),
                        ("ThisPeriodTotalKernelTime", I64), ("TotalPageFaultCount", D), ("TotalProcesses", D),
                        ("ActiveProcesses", D), ("TotalTerminatedProcesses", D)]

        class AcctIo(ctypes.Structure):         # JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION (96 bytes)
            _fields_ = [("BasicInfo", Acct), ("IoInfo", Io)]

        self.Ext, self.AcctIo = Ext, AcctIo
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, ctypes.c_wchar_p], H
        k.SetInformationJobObject.argtypes, k.SetInformationJobObject.restype = [H, ctypes.c_int, ctypes.c_void_p, D], ctypes.c_int
        k.QueryInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, D, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = ctypes.c_int
        k.AssignProcessToJobObject.argtypes, k.AssignProcessToJobObject.restype = [H, H], ctypes.c_int
        k.TerminateJobObject.argtypes, k.TerminateJobObject.restype = [H, D], ctypes.c_int
        k.CloseHandle.argtypes, k.CloseHandle.restype = [H], ctypes.c_int
        self.k = k
        self.h = k.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(f"CreateJobObjectW failed (winerror {ctypes.get_last_error()})")
        info = Ext()
        b = info.BasicLimitInformation
        b.LimitFlags = 0x2000 | 0x400 | 0x8 | 0x2 | (0x100 if mem_mb else 0)   # kill on close, die on crash,
        b.ActiveProcessLimit = procs        # run_python: 1 (no programs); the bench verifier: a few
        b.PerProcessUserTimeLimit = int(cpu_s * 10_000_000)                    # 100 ns units
        if mem_mb:
            info.ProcessMemoryLimit = mem_mb * self.MB
        if not k.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(f"SetInformationJobObject failed (winerror {err})")

    def assign(self, proc) -> None:
        if not self.k.AssignProcessToJobObject(self.h, int(proc._handle)):
            raise OSError(f"AssignProcessToJobObject failed (winerror {self.ct.get_last_error()})")

    def written(self) -> int:
        """Bytes written so far by the job's process: files and its output together."""
        q = self.AcctIo()
        ok = self.k.QueryInformationJobObject(self.h, 8, self.ct.byref(q), self.ct.sizeof(q), None)
        return int(q.IoInfo.WriteTransferCount) if ok else 0

    def peak_mb(self):
        q = self.Ext()
        ok = self.k.QueryInformationJobObject(self.h, 9, self.ct.byref(q), self.ct.sizeof(q), None)
        return int(q.PeakProcessMemoryUsed) // self.MB if ok else None

    def terminate(self, code: int) -> None:
        self.k.TerminateJobObject(self.h, code)

    def close(self) -> None:
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None


_JOB_FACTORY = None          # tests put a stand-in here; when set, run_python takes the job path on any OS
_STATUS_QUOTA_EXCEEDED = 0xC0000044   # what Windows' own CPU-time cap returns (seen in W1 test 5)


def _job_python() -> str:
    """The python.exe to start on the job path: the BASE interpreter. A venv's python.exe is a launcher that starts
    a second process, which the one-process limit would block (W1)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def _job_result(code, timed_out: bool, over_write: bool, peak_mb, text: str, full: bool) -> str:
    """The run_python result on the job path, worded like the POSIX path so the model sees the same messages."""
    if timed_out or code == _STATUS_QUOTA_EXCEEDED:
        return (f"[exit 124]\nTIMED OUT after {RUN_TIMEOUT_S}s and was killed (infinite loop, "
                f"sleep, or waiting for input?).\n{text}".rstrip())
    if over_write:
        return (f"[exit 1] (output reached the {RUN_MAX_WRITE_MB}MB limit: the script wrote far too much and was "
                f"stopped)\n{text}".rstrip())
    # The job's memory cap makes allocation fail (MemoryError) instead of killing; report it like the POSIX watchdog.
    if code not in (0, None) and RUN_MEM_MB and peak_mb is not None and peak_mb >= RUN_MEM_MB - 8:
        return f"[exit 137]\nKILLED: the script used more than {RUN_MEM_MB} MB of memory.\n{text}".rstrip()
    head = f"[exit {code}]"
    if full:
        head += f" (output reached the {RUN_MAX_FILE_MB}MB limit: the script printed far too much)"
    if code is not None and code >= 0xC0000000:
        head += f" (crashed: Windows status {code:#010x}, a resource limit was hit or the interpreter itself crashed)"
        if not text.strip():
            text = "(no output: python died before it could print anything; this is a device/tool problem, not your script)"
    return head + "\n" + (text or "(no output)")


def _run_python_job(target, cfg: dict, env: dict, tmp: str, out) -> str:
    """run_python where POSIX limits do not exist (Windows): the child runs inside a Job Object (_WinJob).
    The child waits for 'go' on stdin, so no line of the script runs before it is inside the job; if the job cannot be
    made or the child cannot be put in it, the script is stopped before it runs (fail closed)."""
    make = _JOB_FACTORY or _WinJob
    try:
        job = make(RUN_MEM_MB, RUN_TIMEOUT_S + 2)    # CPU cap is a backstop only: Windows enforces it late (W1)
    except OSError as e:
        return f"ERROR: run_python could not create its Windows job, so the script was NOT run: {e}"
    env = dict(env, TEMP=tmp, TMP=tmp)               # Windows reads TEMP/TMP, not TMPDIR
    for k in _CHILD_PASSTHROUGH:                     # Windows python needs SystemRoot to start
        if os.environ.get(k):
            env[k] = os.environ[k]
    cfg = dict(cfg, handshake=True, cpu=0, fsize=0)
    timed_out = over_write = False
    try:
        try:
            proc = subprocess.Popen([_job_python(), "-I", "-c", _RUNNER, json.dumps(cfg), str(target)],
                                    cwd=SANDBOX, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env)
        except OSError as e:
            return f"ERROR: could not start python: {e}"
        try:
            job.assign(proc)
        except OSError as e:
            proc.kill()
            proc.wait()
            return f"ERROR: run_python could not put the script into its Windows job, so it was stopped before it ran: {e}"
        try:
            proc.stdin.write(b"go\n")
            proc.stdin.close()                       # the script's own input() now gets end-of-file at once
        except OSError:
            pass
        deadline = time.monotonic() + RUN_TIMEOUT_S
        cap = RUN_MAX_WRITE_MB * 1024 * 1024
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if job.written() > cap:
                over_write = True
                break
            time.sleep(0.05)
        if timed_out or over_write:
            job.terminate(124 if timed_out else 125)  # explicit code: a closed job alone reports exit 0 (W1 test 6)
            proc.wait()
        peak = job.peak_mb()
        full = out.seek(0, 2) >= RUN_MAX_FILE_MB * 1024 * 1024
        text = _head_tail(out)
    finally:
        job.close()
    return _job_result(proc.returncode, timed_out, over_write, peak, text, full)


def cmd_run_python(path_str: str) -> str:
    """Run a .py file inside the sandbox under the audit-hook policy. Output and
    tracebacks come back to the model so it can fix its own code."""
    if not RUN_PYTHON:
        return ("REFUSED: run_python is switched off (RUN_PYTHON=0, or this Python cannot limit a script: "
                "neither POSIX limits nor Windows Job Objects are available here).")
    try:
        _check_path(path_str)
        target = Path(os.path.realpath(SANDBOX / path_str))
        if target.suffix != ".py":
            return "REFUSED: run_python only runs .py files"
        if not target.is_file():
            return f"ERROR: no such file: {path_str}"
        if target in PROTECTED:
            return f"REFUSED: protected file, cannot run: {path_str}"
        problems = scan_python(_read_text(target, "replace"))
    except Refused as e:
        return f"REFUSED: {e}"
    except OSError as e:
        return f"ERROR: {e}"
    if problems:
        return ("REFUSED: not run. These are blocked in the sandbox:\n"
                + "\n".join("  " + p for p in problems[:15]))

    tmp = tempfile.mkdtemp(prefix=".run_", dir=SANDBOX)
    out = tempfile.TemporaryFile()
    cfg = {
        "sandbox": str(SANDBOX), "tmp": os.path.realpath(tmp),
        "protected": [str(p) for p in PROTECTED | READ_ONLY], "read_roots": _read_roots(),
        "cpu": RUN_TIMEOUT_S + 2, "fsize": RUN_MAX_FILE_MB * 1024 * 1024,
    }
    env = {"PATH": SAFE_PATH, "HOME": str(SANDBOX),
           "LC_ALL": "C.UTF-8", "TMPDIR": tmp}
    if _JOB_FACTORY is not None or not _has_posix_limits():   # Windows: Job Object limits (W2)
        try:
            return _run_python_job(target, cfg, env, tmp, out)
        finally:
            out.close()
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _RUNNER, json.dumps(cfg), str(target)],
            cwd=SANDBOX, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
        timed_out = over_mem = False
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if RUN_MEM_MB and (_rss_mb(proc.pid) or 0) > RUN_MEM_MB:
                over_mem = True
                break
            time.sleep(0.05)
        if timed_out or over_mem:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
        full = out.seek(0, 2) >= cfg["fsize"]
        text = _head_tail(out)
    except OSError as e:
        return f"ERROR: could not start python: {e}"
    finally:
        out.close()
        shutil.rmtree(tmp, ignore_errors=True)
    if over_mem:
        return f"[exit 137]\nKILLED: the script used more than {RUN_MEM_MB} MB of memory.\n{text}".rstrip()
    if timed_out:
        return (f"[exit 124]\nTIMED OUT after {RUN_TIMEOUT_S}s and was killed (infinite loop, "
                f"sleep, or waiting for input?).\n{text}".rstrip())
    head = f"[exit {proc.returncode}]"
    if full:
        head += f" (output reached the {RUN_MAX_FILE_MB}MB limit: the script printed far too much)"
    if proc.returncode < 0:
        try:
            name = signal.Signals(-proc.returncode).name
        except ValueError:
            name = f"signal {-proc.returncode}"
        head += f" (killed by {name}: a resource limit was hit, or the interpreter itself crashed)"
        if not text.strip():
            text = "(no output: python died before it could print anything; this is a device/tool problem, not your script)"
    return head + "\n" + (text or "(no output)")


def log_to_index(filename: str, action: str):
    """Logs created or modified files to the tracking index per user specifications."""
    try:
        index_path = SANDBOX / "file_index.txt"
        existing = _read_text(index_path).splitlines() if index_path.exists() else []
        entry = f"{filename} ({action})"
        if entry not in existing:
            existing.append(entry)
            _write_text(index_path, "\n".join(existing) + "\n")
    except Exception:
        pass


# W9: which numbered lines the model has seen, per file, and in which state of the file. replace_lines uses
# line numbers, which go stale after any edit that adds or removes lines; it only runs on lines seen in the
# file's CURRENT state. None = not tracking (one command typed by hand: the human sees the file).
_VIEWS = None


def _view_key(path_str: str) -> str:
    return os.path.normcase(os.path.normpath(path_str))


def _content_sig(text: str) -> str:
    return hashlib.sha256("\n".join(text.splitlines()).encode("utf-8", "surrogatepass")).hexdigest()


def _record_view(path_str: str, text: str, shown) -> None:
    """read_file showed these line numbers of this content: remember them (same content adds lines)."""
    if _VIEWS is None:
        return
    key, sig = _view_key(path_str), _content_sig(text)
    old = _VIEWS.get(key)
    lines = set(shown) | (old[1] if old and old[0] == sig else set())
    _VIEWS[key] = (sig, lines)


def cmd_read_file(path_str: str, start: int = 1, count: int = 40) -> str:
    # 40 lines keeps prompt evaluation fast on ARM CPU
    """Safe built-in tool to read a file with line numbers and optional range slicing."""
    try:
        _check_path(path_str)
        target = SANDBOX / path_str
        if not target.exists():
            return f"REFUSED: file does not exist: {path_str}"

        lines = _read_text(target).splitlines()
        total_lines = len(lines)

        start_idx = max(0, start - 1)
        if total_lines and start_idx >= total_lines:
            return f"ERROR: start line {start} is past the end of {path_str} ({total_lines} lines)"
        selected = lines[start_idx:start_idx + max(1, count)]

        # Cap by characters too, so a few very long lines can't flood the context.
        shown, used = [], 0
        for n, line in enumerate(selected, start=start_idx + 1):
            row = f"{n:4d} | {line}"[:MAX_OUTPUT]
            if shown and used + len(row) > MAX_OUTPUT:
                break
            shown.append(row)
            used += len(row) + 1
        end_idx = start_idx + len(shown)
        _record_view(path_str, "\n".join(lines), range(start_idx + 1, end_idx + 1))

        header = f"[file: {path_str} | lines {start_idx + 1}-{end_idx} of {total_lines}]"
        note = ""
        if end_idx < total_lines:
            note = (f"\n[{total_lines - end_idx} more lines not shown. "
                    f"To continue: read_file {path_str} {end_idx + 1} {count}]")
        return header + "\n" + "\n".join(shown) + note
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def cmd_write_file(path_str: str, content: str) -> str:
    """Safe built-in tool to write or create a new file inside the sandbox."""
    try:
        _check_writable(path_str)
        target = SANDBOX / path_str
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text(target, content)  # written exactly as given (LF, UTF-8): no unescaping
        log_to_index(path_str, "created")
        return f"[exit 0]\nSuccessfully wrote {path_str}"
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _reindent_tail(content, idx, search_text, replace_text):
    """A one-line SEARCH matched after the line's leading spaces, and REPLACE has several
    lines written without that indent: add the file's indent to REPLACE lines 2+."""
    if "\n" in search_text or "\n" not in replace_text:
        return replace_text
    line_start = content.rfind("\n", 0, idx) + 1
    prefix = content[line_start:idx]
    if not prefix or prefix.strip():
        return replace_text
    first, *rest = replace_text.split("\n")
    body = [l for l in rest if l.strip()]
    if not body or min(_indent(l) for l in body) >= len(prefix):
        return replace_text  # already written with the file's indentation
    return "\n".join([first] + [(prefix + l) if l.strip() else l for l in rest])


def _fuzzy_replace(content, search_text, replace_text):
    """Match SEARCH ignoring leading/trailing spaces on each line. Returns new content,
    'AMBIGUOUS', or None. REPLACE is shifted by the difference in indentation."""
    s_lines = search_text.split("\n")
    if not any(l.strip() for l in s_lines):
        return None
    f_lines = content.split("\n")
    n = len(s_lines)
    hits = [i for i in range(len(f_lines) - n + 1)
            if all(f_lines[i + k].strip() == s_lines[k].strip() for k in range(n))]
    if not hits:
        return None
    if len(hits) > 1:
        return "AMBIGUOUS"
    i = hits[0]
    f_ind, s_ind = _indent(f_lines[i]), _indent(s_lines[0])
    pad = f_lines[i][:f_ind]
    if f_ind >= s_ind:
        base = pad[:f_ind - s_ind]
        new = [(base + l) if l.strip() else l for l in replace_text.split("\n")]
    else:  # model indented more than the file: strip the excess where present
        cut = s_ind - f_ind
        new = [(l[cut:] if l[:cut].strip() == "" else l) for l in replace_text.split("\n")]
    return "\n".join(f_lines[:i] + new + f_lines[i + n:])


_GUTTER = re.compile(r"^ *\d+ \|(?: |$)")


def _strip_gutter(text: str):
    """read_file shows '  10 | code'. A small model sometimes copies that column into SEARCH/REPLACE (win-W6-omni2
    run 10: the right fix, refused 7 times as 'not found'). If EVERY non-empty line starts with that column, return
    the text without it; otherwise None (nothing is guessed for a mixed block)."""
    lines = text.split("\n")
    body = [l for l in lines if l.strip()]
    if not body or not all(_GUTTER.match(l) for l in body):
        return None
    return "\n".join(_GUTTER.sub("", l, count=1) for l in lines)


def cmd_patch_file(path_str: str, search_text: str, replace_text: str) -> str:
    """Safe built-in tool to patch an existing file via unique search/replace blocks."""
    try:
        _check_writable(path_str)
        target = SANDBOX / path_str
        if not target.exists():
            return f"REFUSED: file does not exist: {path_str}"
        content = _read_text(target)
        crlf = _uses_crlf(target)   # an edit must not silently turn a CRLF file into LF

        if not search_text:
            return "REFUSED: search text is empty"
        gnote = ""
        if search_text not in content:          # the file's own text always wins; only then look for a copied column
            s2 = _strip_gutter(search_text)
            if s2 is not None and s2.strip():
                search_text = s2
                r2 = _strip_gutter(replace_text)
                if r2 is not None:
                    replace_text = r2
                gnote = (" (the line numbers copied from read_file, like '  10 | ', were removed: never put them in "
                         "SEARCH or REPLACE)")
        if search_text == replace_text:
            return ("REFUSED: SEARCH and REPLACE are identical, so nothing would change. "
                    "Write the corrected lines in REPLACE (keep the file's leading spaces).")
        note = ""
        if search_text in content:
            if content.count(search_text) > 1:
                return f"REFUSED: search text is not unique in {path_str}; include more surrounding lines"
            idx = content.index(search_text)
            fixed = _reindent_tail(content, idx, search_text, replace_text)
            if fixed != replace_text:
                note = " (indentation of the new lines adjusted to match the file)"
            new_content = content[:idx] + fixed + content[idx + len(search_text):]
        else:
            hit = _fuzzy_replace(content, search_text, replace_text)
            if hit is None:
                return (f"REFUSED: search text not found in {path_str} (it must match exactly, "
                        f"including indentation). Use read_file {path_str} to copy the exact lines.")
            if hit == "AMBIGUOUS":
                return f"REFUSED: search text matches more than one place in {path_str}; include more surrounding lines"
            new_content, note = hit, " (leading spaces adjusted to match the file)"
        if new_content == content:
            return "REFUSED: that edit would not change the file"
        if path_str.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError:
                pass  # the file was already broken: let the model repair it step by step
            else:
                try:
                    ast.parse(new_content)
                except SyntaxError as e:
                    bad = (e.text or "").strip()
                    return (f"REFUSED: this patch would leave {path_str} with a syntax error "
                            f"(line {e.lineno}: {e.msg}{'  -> ' + bad if bad else ''}), so it was NOT applied and the file "
                            f"is unchanged. A REPLACE must keep the brackets, quotes and indentation balanced: if SEARCH "
                            f"opens a '{{' or '(' then REPLACE must close it too. Use read_file {path_str} to copy the "
                            f"exact lines, then send a corrected patch.")
        _write_text(target, new_content, crlf)
        log_to_index(path_str, "patched")
        return f"[exit 0]\nSuccessfully patched {path_str}{note}{gnote}"
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _around(first: int, last: int, total: int):
    """(start, count) for read_file: lines first..last with 3 lines of context, at most 40 lines."""
    start = max(1, first - 3)
    return start, max(1, min(40, min(total, last + 3) - start + 1))


def cmd_replace_lines(path_str: str, first: int, last: int, new_text: str) -> str:
    """W9: replace lines first..last (the numbers read_file shows, inclusive) with new_text. An empty new_text
    deletes them. Only runs on lines the model has seen in the file's current state (see _VIEWS)."""
    try:
        _check_writable(path_str)
        target = SANDBOX / path_str
        if not target.is_file():
            return f"REFUSED: file does not exist: {path_str}"
        content = _read_text(target)
        crlf = _uses_crlf(target)
        lines = content.split("\n")
        trail = content.endswith("\n")
        if trail:
            lines = lines[:-1]
        total = len(lines)
        if not (1 <= first <= last <= total):
            s, c = _around(min(max(first, 1), total), min(max(last, 1), total), total) if total else (1, 1)
            return (f"REFUSED: {path_str} has {total} lines, so lines {first}-{last} do not exist (the first number "
                    f"must be 1 or more and not larger than the second). Its lines are below:\n"
                    + cmd_read_file(path_str, s, c))
        if _VIEWS is not None:
            v = _VIEWS.get(_view_key(path_str))
            if v is None or v[0] != _content_sig(content):
                why = ("you have not read it with read_file yet" if v is None else
                       "it has changed since you last read it, so its line numbers have moved")
            else:
                missing = [n for n in range(first, last + 1) if n not in v[1]]
                why = f"you have not seen line {missing[0]} of it yet" if missing else ""
            if why:
                s, c = _around(first, last, total)
                return (f"REFUSED: replace_lines on {path_str} was NOT applied: {why}, and line numbers are only "
                        f"safe on lines you have seen. Its current lines are below. Send replace_lines again in your "
                        f"next reply, with these numbers:\n" + cmd_read_file(path_str, s, c))
        new = new_text[:-1] if new_text.endswith("\n") else new_text
        note = ""
        g = _strip_gutter(new)
        if g is not None:
            new, note = g, (" (the line numbers copied from read_file, like '  10 | ', were removed: write only the "
                            "code)")
        new_lines = new.split("\n") if new != "" else []
        old_ws = lines[first - 1][:_indent(lines[first - 1])]
        body = [l for l in new_lines if l.strip()]
        if body and old_ws and min(_indent(l) for l in body) == 0:
            new_lines = [old_ws + l if l.strip() else l for l in new_lines]
            note += " (indentation of the new lines adjusted to match the file)"
        if new_lines == lines[first - 1:last]:
            return ("REFUSED: those lines already have exactly that text, so nothing would change. Write the "
                    "corrected lines in the ``` block")
        out = lines[:first - 1] + new_lines + lines[last:]
        new_content = "\n".join(out) + ("\n" if trail and out else "")
        if path_str.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError:
                pass  # already broken: let the model repair it step by step
            else:
                try:
                    ast.parse(new_content)
                except SyntaxError as e:
                    bad = (e.text or "").strip()
                    return (f"REFUSED: this edit would leave {path_str} with a syntax error (line {e.lineno}: "
                            f"{e.msg}{'  -> ' + bad if bad else ''}), so it was NOT applied and the file is "
                            f"unchanged. Keep the brackets, quotes and indentation balanced.")
        _write_text(target, new_content, crlf)
        log_to_index(path_str, "patched")
        n_new = len(new_lines)
        s, c = _around(first, first + max(n_new, 1) - 1, max(len(out), 1))
        view = cmd_read_file(path_str, s, c) if out else f"[{path_str} is now empty]"
        what = (f"deleted lines {first}-{last} of {path_str}" if not new_lines else
                f"replaced lines {first}-{last} of {path_str} with {n_new} line{'s' if n_new != 1 else ''}")
        moved = "" if n_new == last - first + 1 else " Line numbers below this edit have changed."
        return (f"[exit 0]\nSuccessfully {what}{note}.{moved} The file now reads, around your edit:\n"
                f"{view}")
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _find_def(tree, dotted: str):
    """The AST node for 'func', 'Class' or 'Class.method' (nesting allowed), else Refused."""
    scope, node, parts = tree, None, dotted.split(".")
    for depth, name in enumerate(parts):
        kids = [n for n in ast.iter_child_nodes(scope)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        hits = [n for n in kids if n.name == name]
        where = ".".join(parts[:depth + 1])
        if not hits:
            have = ", ".join(n.name for n in kids) or "nothing"
            raise Refused(f"no def or class named {where} in that file. At that level it defines: {have}. "
                          f"Use read_file to see the names (for a method write Class.method)")
        if len(hits) > 1:
            lines = ", ".join(str(n.lineno) for n in hits)
            raise Refused(f"{where} is defined {len(hits)} times (lines {lines}), so replace_def cannot tell which "
                          f"one you mean. Use patch_file with enough surrounding lines instead")
        scope = node = hits[0]
    return node


def cmd_replace_def(path_str: str, name: str, new_code: str) -> str:
    """Replace ONE whole function / method / class in a .py file with new_code (the complete new
    definition). The old version is found with ast, so the model copies no old text and counts no
    lines. Small models treat SEARCH/REPLACE as 'insert after this line' and leave the old body behind."""
    try:
        _check_writable(path_str)
        if not path_str.endswith(".py"):
            return "REFUSED: replace_def only works on .py files; use patch_file for other files"
        target = SANDBOX / path_str
        if not target.is_file():
            return f"REFUSED: file does not exist: {path_str}"
        content = _read_text(target)
        crlf = _uses_crlf(target)   # an edit must not silently turn a CRLF file into LF
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            return (f"REFUSED: {path_str} already has a syntax error (line {e.lineno}: {e.msg}), so its functions "
                    f"cannot be located. Repair that line with patch_file first")
        node = _find_def(tree, name)
        short = name.split(".")[-1]

        code = textwrap.dedent(new_code).strip("\n")
        try:
            new_tree = ast.parse(code)
        except SyntaxError as e:
            return (f"REFUSED: your new code is not valid Python (line {e.lineno}: {e.msg}), so nothing was changed. "
                    f"The ``` block must hold the complete new {short} and nothing else")
        top = new_tree.body
        if (len(top) != 1 or not isinstance(top[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))):
            return (f"REFUSED: the ``` block must contain exactly ONE complete def or class (the new {short}) and "
                    f"nothing else: no imports, no other functions, no test code. Nothing was changed. "
                    f"Use patch_file to change other lines")
        if top[0].name != short:
            return (f"REFUSED: the block defines '{top[0].name}' but you asked to replace '{short}'. "
                    f"Keep the same name, or nothing was changed")
        if isinstance(top[0], ast.ClassDef) != isinstance(node, ast.ClassDef):
            return f"REFUSED: {name} is a {'class' if isinstance(node, ast.ClassDef) else 'function'} but the block defines the other kind"

        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno
        new_text = textwrap.indent(code, " " * node.col_offset) + "\n"
        lines = content.splitlines(keepends=True)
        new_content = "".join(lines[:start - 1]) + new_text + "".join(lines[end:])
        if new_content == content:
            return "REFUSED: that is the same code the file already has, so nothing would change"
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return (f"REFUSED: this would leave {path_str} with a syntax error (line {e.lineno}: {e.msg}), so it was "
                    f"NOT applied and the file is unchanged")
        _write_text(target, new_content, crlf)
        log_to_index(path_str, "patched")
        return (f"[exit 0]\nSuccessfully replaced {name} in {path_str} "
                f"(old lines {start}-{end} are now {len(new_text.splitlines())} lines)")
    except Refused as e:
        return f"REFUSED: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _expand_paths(a: str) -> list[str]:
    if any(c in a for c in "*?["):
        matches = sorted(glob.glob(a, root_dir=SANDBOX))[:MAX_GLOB]
        if matches:
            for m in matches:
                _check_path(m)
            return matches
    _check_path(a)
    return [a]


def _resolve_long(name: str, table: dict) -> str:
    if name in table:
        return name
    hits = [k for k in table if name and k.startswith(name)]
    if len(hits) == 1:
        return hits[0]
    raise Refused(f"unsupported or ambiguous flag: --{name}")


def validate(argv: list[str]) -> list[str]:
    if not argv:
        raise Refused("empty command")
    cmd, args = argv[0], argv[1:]
    if cmd not in POLICY:
        raise Refused(f"command not allowed: {cmd} (allowed: {', '.join(POLICY)}, read_file, write_file, patch_file, replace_def, replace_lines, check_file, run_python)")
    pol = POLICY[cmd]
    for a in args:
        if a in SHELL_TOKENS:
            raise Refused("no pipes/redirects/chaining; run one command at a time")

    need_pattern = pol["pattern"]
    out, i, dashdash = [], 0, False
    inplace, paths = False, []
    while i < len(args):
        a = args[i]
        i += 1
        if not dashdash and a == "--":
            dashdash = True
            out.append(a)
            continue
        if not dashdash and a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            key = _resolve_long(name, pol["long"])
            kind = pol["long"][key]
            if kind == _I and not ALLOW_SED_INPLACE:
                raise Refused("sed in-place editing is disabled")
            if kind == _I:
                inplace = True
            if kind in (_F, _I):
                out.append(f"--{key}{eq}{val}")
            else:
                if not eq:
                    if i >= len(args):
                        raise Refused(f"--{key} needs a value")
                    val = args[i]
                    i += 1
                out.append(f"--{key}={val}")
                if kind == _P:
                    need_pattern = False
            continue
        if not dashdash and a.startswith("-") and a != "-":
            chars, extra = a[1:], []
            for idx, ch in enumerate(chars):
                if ch in pol["short_deny"]:
                    raise Refused(f"flag not allowed: -{ch}")
                if cmd == "sed" and ch == "i":
                    if not ALLOW_SED_INPLACE:
                        raise Refused("sed in-place editing is disabled")
                    inplace = True
                    break
                if ch in pol["short_val"]:
                    if ch in pol["pattern_short"]:
                        need_pattern = False
                    if idx == len(chars) - 1:
                        if i >= len(args):
                            raise Refused(f"-{ch} needs a value")
                        extra.append(args[i])
                        i += 1
                    break
            out.append(a)
            out.extend(extra)
            continue
        if need_pattern:
            need_pattern = False
            out.append(a)
            continue
        expanded = _expand_paths(a)
        paths.extend(expanded)
        out.extend(expanded)

    if inplace:
        for p in paths:
            _check_writable(p)
    if cmd == "sed":
        out.insert(0, "--sandbox")
    return [cmd] + out


_NO_WRITE_WORKAROUND = ("To create or change a file use write_file / patch_file with a ``` block "
                        "after the CMD line. To run Python use: run_python <file.py>.")


def _hint(command: str, msg: str) -> str:
    """Tell the model what to do instead, so a refusal is not a dead end."""
    low = (command or "").lower()
    first = low.split(None, 1)[0] if low.split() else ""
    if ("no pipes" in msg or "No closing quotation" in msg or "No escaped" in msg
            or first in ("python", "python3", "bash", "sh", "node", "perl", "ruby", "base64",
                         "echo", "printf", "tee", "cp", "mv", "touch", "dd", "xxd", "openssl")
            or "base64" in low or "<<" in low):
        return msg + "\n" + _NO_WRITE_WORKAROUND
    return msg


def _audit(command: str, msg: str):
    """Append a refused command to AUDIT_LOG (protected, outside the model's reach)."""
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg[:120]!r}\t{command[:300]!r}\n")
    except OSError:
        pass


def run(command: str, body: str = "") -> str:
    out = _run(command, body)
    if out.startswith("REFUSED:"):
        _audit(command, out)
        out = _hint(command, out)
    return out


def _run(command: str, body: str = "") -> str:
    """Tool entry point. Intercepts custom built-ins or runs whitelisted CLI commands.

    `body` is the text that followed the CMD line in the model's reply. write_file
    takes its content from a ``` block in it and patch_file from a SEARCH/REPLACE
    block, so code never needs escaping."""
    try:
        parts = shlex.split(command)
        if not parts:
            return "REFUSED: empty command"

        cmd = parts[0]

        # Intercept custom built-in file reading/writing/patching tools
        if cmd == "read_file":
            parts_len = len(parts)
            path = parts[1] if parts_len > 1 else ""
            try:
                start = int(parts[2]) if parts_len > 2 else 1
            except ValueError:
                start = 1
            try:
                count = int(parts[3]) if parts_len > 3 else 150
            except ValueError:
                count = 150
            return cmd_read_file(path, start, count)
        elif cmd == "run_python":
            if len(parts) != 2:
                return "REFUSED: usage: run_python <path.py>"
            return cmd_run_python(parts[1])
        elif cmd == "check_file":
            if len(parts) != 2:
                return "REFUSED: usage: check_file <path.py>"
            return cmd_check_file(parts[1])
        elif cmd == "write_file":
            if len(parts) < 2:
                return "REFUSED: write_file requires <path>"
            block = _fenced_block(body)
            if block is not None:
                return _with_check(cmd_write_file(parts[1], block), parts[1])
            inline = " ".join(parts[2:])
            if inline and "\\n" not in inline:  # short single-line content is fine inline
                return _with_check(cmd_write_file(parts[1], inline), parts[1])
            return ("REFUSED: no file contents received. The ``` block must come on the lines "
                    "AFTER the CMD line, not before it (no escaping needed)")
        elif cmd == "replace_def":
            if len(parts) != 3:
                return ("REFUSED: usage: replace_def <path.py> <name>   (name = a function, a class, or Class.method), "
                        "then the complete new definition in a ``` block on the lines after the CMD line")
            block = _fenced_block(body)
            if block is None:
                return ("REFUSED: no code received. The ``` block holding the whole new function must come on the "
                        "lines AFTER the CMD line")
            return _with_check(cmd_replace_def(parts[1], parts[2], block), parts[1])
        elif cmd == "replace_lines":
            usage = ("REFUSED: usage: replace_lines <path> <first> <last>   (the line numbers read_file shows), then "
                     "the new lines in a ``` block on the lines after the CMD line")
            if len(parts) not in (3, 4) or not all(p.isdigit() for p in parts[2:]):
                return usage
            first = int(parts[2])
            last = int(parts[3]) if len(parts) == 4 else first
            block = _fenced_block(body)
            if block is None:
                return ("REFUSED: no new lines received. The ``` block holding the new lines must come on the lines "
                        "AFTER the CMD line (an empty ``` block deletes the lines)")
            return _with_check(cmd_replace_lines(parts[1], first, last, block), parts[1])
        elif cmd == "patch_file":
            if len(parts) < 2:
                return "REFUSED: patch_file requires <path>"
            sr = _search_replace(body)
            if sr is not None:
                if sr[0] == sr[1] and _text_after_block(body):
                    return ("REFUSED: SEARCH and REPLACE are identical, and code was found AFTER the "
                            ">>>>>>> REPLACE line, where it is ignored. The new lines go INSIDE the "
                            "block, between ======= and >>>>>>> REPLACE:\n"
                            "<<<<<<< SEARCH\nold lines\n=======\nnew lines\n>>>>>>> REPLACE"
                            + _replace_def_hint(parts[1], sr[0]))
                if sr[0] == sr[1]:
                    _hint_rd = _replace_def_hint(parts[1], sr[0])
                    if _hint_rd:
                        return ("REFUSED: SEARCH and REPLACE are identical, so nothing would change." + _hint_rd)
                return _with_check(cmd_patch_file(parts[1], sr[0], sr[1]), parts[1])
            if len(parts) >= 4 and "\\n" not in parts[2] + parts[3]:
                return _with_check(cmd_patch_file(parts[1], parts[2], parts[3]), parts[1])
            return ("REFUSED: no edit received. The SEARCH/REPLACE block must come on the lines "
                    "AFTER the CMD line, not before it:\n"
                    "<<<<<<< SEARCH\nold lines\n=======\nnew lines\n>>>>>>> REPLACE")

        # Otherwise validate and run standard whitelisted CLI tool
        argv = validate(parts)
    except (Refused, ValueError) as e:
        return f"REFUSED: {e}"

    exe = _find_exe(argv[0])
    if exe is None:
        return (f"ERROR: {argv[0]} is not installed on this machine, so this command cannot run here. "
                f"Use read_file <path> [start] [count] to read a file; run_python and check_file do not need it.")
    try:
        r = subprocess.run(
            [exe] + argv[1:], cwd=SANDBOX, stdin=subprocess.DEVNULL,
            capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {TIMEOUT_S}s"
    except FileNotFoundError:
        return (f"ERROR: {argv[0]} is not installed on this machine, so this command cannot run here. "
                f"Use read_file <path> [start] [count] to read a file; run_python and check_file do not need it.")
    text = (r.stdout + r.stderr) or "(no output)"
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n...[truncated at {MAX_OUTPUT} chars]"
    return f"[exit {r.returncode}]\n{text}"


# ---------------------------------------------------------------- agent loop
SYSTEM = """You are a coding assistant with restricted terminal and file tools.
Every reply is an optional one-line note followed by ONE command:
NOTE: <one line: what you just learned (file + line numbers) or what you plan next>
CMD: <command>

Your NOTE lines are your memory. Old tool output is deleted to save space, but your
own replies are kept, so write every important finding into a NOTE.

Commands (paths are relative to the sandbox root, no pipes or redirects):
- read_file <path> [start_line] [count]   reads a file with line numbers
- cat, grep, sed, ls, head, tail, wc      standard tools
- write_file <path>   creates/overwrites a file. Put the whole file in a ``` block on
  the lines after the CMD line. Write code normally, no escaping.
- patch_file <path>   edits a few lines. Put a SEARCH/REPLACE block after the CMD line.
- replace_def <path.py> <name>   replaces ONE whole function (or class, or Class.method) with the
  new version you write. Put the COMPLETE new definition in a ``` block after the CMD line. The
  old version is removed for you: no SEARCH text and no line numbers. Use it to fix a function.
- replace_lines <path> <first> <last>   replaces lines first to last (the numbers read_file
  showed you) with the lines in a ``` block after the CMD line. Write only the code, never the
  '  10 | ' numbers. Use it to change one or a few lines you have just read.
- check_file <path.py>   static check (syntax, stub bodies, shadowed methods, wrong
  argument counts). It never runs the code. It also runs automatically after every
  write_file/patch_file/replace_def of a .py file: if it lists problems, fix them
  before you reply DONE.
- run_python <path.py>   runs a .py file in the sandbox and returns its output or its
  traceback. Limits: 10 seconds, no keyboard input, no network, cannot delete files, cannot
  start other programs (no subprocess, os.system, multiprocessing), cannot use paths
  containing '..', reads and writes only inside the sandbox.

WORKFLOW for code: write_file, then run_python, then fix any traceback with replace_def (a whole
function) or patch_file (a few lines) and run_python again. Only say the code works if run_python showed [exit 0] and the output
matches the task. If it still fails, say so and quote the error.

FILES: never create or edit files with cat, echo, printf, heredocs, redirects, python -c,
base64 or any other trick. Those are refused. Only write_file, patch_file, replace_def and replace_lines write files, and
only run_python runs code.

RULE OF OBSERVATION: you cannot know your code works until run_python has run it and you
have read the output. Never declare success, and never patch, on a guess. After any
write_file, patch_file, replace_def or replace_lines of a .py file, run_python it before you reply DONE.

When you see a NameError for a standard library module, always place the import statement
at the very top of the file (module scope), not inline.

A patch only counts when the tool answers "Successfully patched". If a patch or write was
REFUSED, fix the problem and send it again before you say DONE. Every line you put in a
REPLACE block needs the same leading spaces the file has.

CODE RULES (when asked to write Python):
- Write real, working logic. Never leave 'pass', '...', or '# TODO' bodies unless the
  user asks for a skeleton.
- Use the names, fields and behaviours the task spells out. If it names a state dict,
  scoring rule or print messages, implement exactly those.
- Never give an attribute the same name as a method (self.score = ... next to def score).
- Put demo code under: if __name__ == "__main__":

Example write_file:
CMD: write_file hello.py
```python
print("hi\\n")
```

Example patch_file (SEARCH must match exactly and be unique: include 2-3 nearby lines):
CMD: patch_file hello.py
<<<<<<< SEARCH
print("hi\\n")
=======
print("hello\\n")
>>>>>>> REPLACE

Example replace_def (the new text replaces the whole old function, and its name stays the same):
CMD: replace_def hello.py greet
```python
def greet(name):
    return "hello " + name
```

Example replace_lines (read_file showed "   1 | print("hi\\n")"; this replaces line 1):
CMD: replace_lines hello.py 1 1
```python
print("hello\\n")
```

If a file is longer than what you read, say which lines you reviewed and make no
claims about lines you have not seen (use the 'To continue' hint to read more).
When done, reply: DONE: <answer>   (build the answer from your NOTE lines)"""

MODEL = os.environ.get("LLAMA_MODEL", "local-model")  # llama-server ignores the name; LM Studio/Ollama need the real one
BACKEND = os.environ.get("BACKEND", "local").lower()      # local | gemini
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
LLAMA_TIMEOUT = int(os.environ.get("LLAMA_TIMEOUT", "900"))  # longest SILENCE from the server (prompt reading counts)
LLAMA_TOTAL_TIMEOUT = int(os.environ.get("LLAMA_TOTAL_TIMEOUT", "1800"))  # cap for one whole reply
SERVER_URL = os.environ.get("LLAMA_URL", "http://localhost:8080/v1/chat/completions")


def ask_model(msgs) -> str:
    # Context Pruning: Keep only the latest tool output at full size,
    # and truncate older tool outputs to save precious context tokens.
    trimmed_msgs = []
    total_msgs = len(msgs)

    for idx, (role, content) in enumerate(msgs):
        # Check if this is an older user message containing tool output
        if role == "user" and "Here is the output from running" in content and idx < total_msgs - 2:
            # Grab just the first line (e.g., the file header) as a mini summary
            first_line = content.splitlines()[0] if content.splitlines() else "tool output"
            trimmed_msgs.append((role, f"{first_line}\n[older tool output truncated for context efficiency]"))
        else:
            trimmed_msgs.append((role, content))

    if BACKEND == "gemini":
        return _ask_gemini(trimmed_msgs)
    return _ask_local(trimmed_msgs)


def _ask_gemini(msgs) -> str:
    """Gemini via google-genai. The SYSTEM prompt goes in system_instruction, and
    assistant turns are sent with role 'model', so the model sees real roles."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return "ERROR: Failed to connect: GEMINI_API_KEY environment variable is not set."
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "ERROR: Failed to connect: run  pip install google-genai"
    system = "\n\n".join(c for r, c in msgs if r == "system")
    contents = [types.Content(role="model" if r == "assistant" else "user",
                              parts=[types.Part(text=c)])
                for r, c in msgs if r != "system"]
    config = types.GenerateContentConfig(system_instruction=system, temperature=0.2)
    client = genai.Client(api_key=api_key)
    for attempt in range(5):
        try:
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=contents, config=config)
            return (resp.text or "").strip()
        except Exception as e:
            if attempt == 4:
                return f"ERROR: Failed to connect to Gemini API: {e}"
            wait = 5 * (attempt + 1)
            print(f"[info] Gemini busy, retrying in {wait}s (attempt {attempt + 1}/5)...")
            time.sleep(wait)


LAST_STATS = ""   # one line about the last local model call (speed), shown after each reply
LLAMA_STREAM = os.environ.get("LLAMA_STREAM", "1") != "0"   # 0 = plain request, like curl


NOTE_MAX_CHARS = 300   # a NOTE: is one short line; longer ones are cut (runaway "1, 2, 3 ..." loops)
NOTE_MAX_LINES = 3     # a reply keeps at most this many NOTE: lines (bugfix run 3: 75 of them in one reply)


def _note_lines_outside_fences(text: str) -> int:
    n, in_fence = 0, False
    for line in text.splitlines():
        t = line.strip()
        if t.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and t.startswith("NOTE:"):
            n += 1
    return n


def _runaway_note(text: str) -> bool:
    """True if the reply so far ends inside a NOTE: line that is already far too long.
    Small models sometimes count 1, 2, 3 ... forever; stopping the stream saves minutes on a phone.
    Lines inside a ``` block (file content) are never treated as notes."""
    if text.count("```") % 2:
        return False
    if _note_lines_outside_fences(text) > 2 * NOTE_MAX_LINES:   # many short NOTE lines, one per line
        return True
    last = text.rsplit("\n", 1)[-1].strip()
    return last.startswith("NOTE:") and len(last) > 2 * NOTE_MAX_CHARS


def _cap_notes(reply: str) -> str:
    """Cut NOTE: lines longer than NOTE_MAX_CHARS and keep only the first NOTE_MAX_LINES of them
    (outside ``` blocks; file content is never touched)."""
    out, in_fence, kept = [], False, 0
    for line in reply.splitlines():
        t = line.strip()
        if t.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and t.startswith("NOTE:"):
            kept += 1
            if kept > NOTE_MAX_LINES:
                continue
            if len(t) > NOTE_MAX_CHARS:
                line = t[:NOTE_MAX_CHARS].rstrip() + " ..."
        out.append(line)
    return "\n".join(out)


class _BadReply(Exception):
    """The server answered, but not in a form we can read."""


def _read_sse(lines, show_progress=True):
    """Parse server-sent-event lines. Tolerates blank 'data:' lines, comments, CRLF and
    chunks that are not JSON. Returns (text, n_pieces, t_first, timings, skipped)."""
    parts, n, t_first, timings, skipped = [], 0, None, None, []
    t0 = time.time()
    for raw in lines:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk:
            continue
        if chunk == "[DONE]":
            break
        try:
            d = json.loads(chunk)
        except ValueError:
            skipped.append(chunk[:120])
            continue
        if not isinstance(d, dict):
            continue
        if "error" in d and not d.get("choices"):
            raise _BadReply(f"server error: {str(d['error'])[:200]}")
        timings = d.get("timings") or timings
        for c in d.get("choices") or []:
            piece = (c.get("delta") or {}).get("content") or (c.get("message") or {}).get("content")
            if piece:
                if t_first is None:
                    t_first = time.time()
                parts.append(piece)
                n += 1
                if n % 20 == 0 and _runaway_note("".join(parts)):
                    return "".join(parts), n, t_first, timings, skipped
                if show_progress and n % 5 == 0 and sys.stderr.isatty():
                    rate = n / max(time.time() - t_first, 1e-6)
                    sys.stderr.write(f"\r[llm] generating: {n} tokens ({rate:.2f} tok/s)  ")
                    sys.stderr.flush()
        if time.time() - t0 > LLAMA_TOTAL_TIMEOUT:
            raise TimeoutError(
                f"too slow: {n} tokens in {int(time.time() - t0)}s (limit "
                f"LLAMA_TOTAL_TIMEOUT={LLAMA_TOTAL_TIMEOUT}s). Run: python sandbox_tool.py --ping")
    return "".join(parts), n, t_first, timings, skipped


def _ask_local(trimmed_msgs, max_tokens=1024, stream=None) -> str:
    """Local llama-server. Streamed by default: LLAMA_TIMEOUT is the longest the server may
    stay SILENT (a slow phone can need minutes to read the prompt before the first token),
    and LLAMA_TOTAL_TIMEOUT caps one whole reply. If the streamed reply cannot be read, it
    asks again once without streaming (the plain request curl uses)."""
    global LAST_STATS
    LAST_STATS = ""
    stream = LLAMA_STREAM if stream is None else stream
    payload = {
        "model": MODEL,
        "messages": [{"role": role, "content": content} for role, content in trimmed_msgs],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(
        SERVER_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0, t_first, n, timings, text = time.time(), None, 0, None, ""
    try:
        with urllib.request.urlopen(req, timeout=LLAMA_TIMEOUT) as r:
            if stream:
                text, n, t_first, timings, skipped = _read_sse(r)
                if not text:  # the body may be a plain JSON reply, or an SSE body sent as JSON
                    raise _BadReply("empty streamed reply" + (f" (unreadable chunk: {skipped[0]!r})" if skipped else ""))
            else:
                body = r.read().decode("utf-8", "replace")
                if not body.strip():
                    raise _BadReply("the server sent an empty reply")
                if body.lstrip().startswith("data:"):
                    text, n, t_first, timings, _ = _read_sse(body.splitlines())
                else:
                    try:
                        data = json.loads(body)
                        text = data["choices"][0]["message"]["content"] or ""
                        timings = data.get("timings")
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise _BadReply(f"unreadable reply from the server: {body[:200]!r}")
    except _BadReply as e:
        if stream:   # one more try the plain way, before giving up
            sys.stderr.write(f"[llm] streamed reply unreadable ({e}); retrying without streaming\n")
            return _ask_local(trimmed_msgs, max_tokens, stream=False)
        return f"ERROR: Failed to connect to local llama-server: {e}"
    except Exception as e:
        hint = ""
        if "timed out" in str(e) or isinstance(e, TimeoutError):
            hint = "  (the server is too slow or stuck; run: python sandbox_tool.py --ping)"
        return f"ERROR: Failed to connect to local llama-server: {e}{hint}"
    finally:
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 60 + "\r")
    total = time.time() - t0
    bits = [f"{total:.0f}s total"]
    if timings:
        pn = timings.get("prompt_n") or 0
        if 0 < pn < 20 and timings.get("cache_n"):   # almost all of the prompt came from the server's cache
            bits.append(f"prompt cached ({timings['cache_n']} tok reused, {pn} new; read speed not measured)")
        elif pn:
            bits.append(f"prompt {pn} tok @ {timings.get('prompt_per_second', 0):.1f} tok/s")
        if timings.get("predicted_n"):
            bits.append(f"generated {timings['predicted_n']} tok @ {timings.get('predicted_per_second', 0):.2f} tok/s")
    elif t_first and n > 1:
        bits.append(f"generated ~{n} tok @ {(n - 1) / max(time.time() - t_first, 1e-6):.2f} tok/s")
    LAST_STATS = ", ".join(bits)
    return text.strip()


def ping() -> str:
    """Time one real-sized request so you can see how fast the local server is."""
    msgs = [("system", SYSTEM), ("user", "Reply with exactly: CMD: ls")]
    t0 = time.time()
    out = _ask_local(msgs, max_tokens=24)
    if out.startswith("ERROR:"):
        return out
    est = LAST_STATS
    return (f"server replied in {time.time() - t0:.0f}s: {out!r}\n[llm] {est}\n"
            "If it says 'prompt cached', the server already had this prompt, so only generation speed was measured.\n"
            "Rule of thumb: the first agent step reads the whole system prompt "
            f"(~{len(SYSTEM) // 4} tokens) once; later steps reuse the server's cache. "
            "Below ~1 tok/s generation, check CPU throttling, free RAM, leftover llama processes, "
            "and the -t (threads) / -np (slots) server flags.")


BLOCK_CMDS = ("write_file", "patch_file", "replace_def", "replace_lines")
EDIT_CMDS = ("patch_file", "replace_def", "replace_lines")   # change part of an existing file


def parse_reply(reply: str):
    """Parse a model reply into (kind, text, body, notes).

    kind  'cmd' | 'done' | None
    text  the command line, or the full DONE answer (every line after the marker)
    body  lines after the CMD line (file content / SEARCH-REPLACE block), else ''
    notes the NOTE: lines. They are read before the marker; after a CMD line only
          for ordinary commands, so a 'NOTE:' inside file content is never a note.
    """
    lines = _cap_notes(reply).splitlines()
    notes = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("NOTE:"):
            notes.append(s[5:].strip())
        elif s.startswith("DONE:"):
            return "done", "\n".join([s[5:].strip()] + lines[idx + 1:]).strip(), "", notes
        elif s.startswith("CMD:"):
            text = s[4:].strip()
            if text.startswith("DONE:"):  # small models write "CMD: DONE: ..."
                return "done", "\n".join([text[5:].strip()] + lines[idx + 1:]).strip(), "", notes
            if re.fullmatch(r"DONE[.!]?", text, re.I):  # ... or just "CMD: DONE" (100-run csv_total: 2 correct runs lost)
                return "done", "\n".join([notes[-1] if notes else "(finished)"] + lines[idx + 1:]).strip(), "", notes
            body = "\n".join(lines[idx + 1:])
            if text.split(" ", 1)[0] not in BLOCK_CMDS:
                notes += [l.strip()[5:].strip() for l in lines[idx + 1:]
                          if l.strip().startswith("NOTE:")]
            return "cmd", text, body, notes
    return None, "", "", notes


MAX_BATCH = max(1, int(os.environ.get("MAX_BATCH", "3") or 3))  # commands run per reply (1 = the old one-at-a-time loop)


def split_commands(reply: str):
    """Every command in a reply, in order: ([(command_line, body, raw), ...], done_ignored).

    body  the lines belonging to that command, up to the next command (file content / a SEARCH-REPLACE block)
    raw   what goes into history for it: just the CMD line, or the CMD line plus its block
    done_ignored  a DONE came after a command. A reply that ran commands never ends the run; the DONE
                  was written before any real output existed.
    CMD:/DONE: inside a code fence or a block is file content. Lines between commands (an invented
    'NOTE: it works', pretend tool output in a fence) are not commands and are dropped."""
    lines = _cap_notes(reply).splitlines()
    starts, done_ignored, in_fence, i = [], False, False, 0
    while i < len(lines):
        t = lines[i].strip()
        if t.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and t.startswith("DONE:"):
            done_ignored = bool(starts)
            break
        elif not in_fence and t.startswith("CMD:"):
            text = t[4:].strip()
            if text.startswith("DONE:") or re.fullmatch(r"DONE[.!]?", text, re.I):
                done_ignored = bool(starts)
                break
            starts.append((i, text))
            if text.split(" ", 1)[0] in BLOCK_CMDS:
                i += len(_cut_after_block("\n".join(lines[i:]), text).splitlines())
                continue
        i += 1
    segs = []
    for k, (ci, text) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body = "\n".join(lines[ci + 1:end])
        raw = (_cut_after_block("\n".join(lines[ci:end]), text)
               if text.split(" ", 1)[0] in BLOCK_CMDS else f"CMD: {text}")
        segs.append((text, body, raw))
    return segs, done_ignored


def _preview(result: str) -> str:
    """Terminal display only. The model always receives the full result."""
    lines = result.splitlines()
    if PREVIEW_LINES <= 0 or len(lines) <= PREVIEW_LINES:
        return result
    hidden = len(lines) - PREVIEW_LINES
    return ("\n".join(lines[:PREVIEW_LINES])
            + f"\n... [{hidden} more lines were sent to the model; PREVIEW_LINES=0 shows all]")


def _local_imports(path_str: str, seen=None) -> set:
    """Names of sandbox .py files that `path_str` imports (directly or through each other)."""
    seen = seen if seen is not None else set()
    try:
        tree = ast.parse(_read_text(SANDBOX / path_str, "replace"))
    except (OSError, SyntaxError, ValueError):
        return seen
    for n in ast.walk(tree):
        mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                else [n.module] if isinstance(n, ast.ImportFrom) and n.module and not n.level else [])
        for m in mods:
            f = m.split(".")[0] + ".py"
            if f not in seen and (SANDBOX / f).is_file():
                seen.add(f)
                _local_imports(f, seen)
    return seen


def _entry_points(target: str) -> list:
    """Sandbox .py files that import `target` (directly or through other files) and that no other sandbox file
    imports in turn: the file to run (main.py) when `target` only defines code. Top-level files only, never
    `target` itself or a PROTECTED file. pipeline_omni v2 (update 24): after an edit the model ran the module it had
    edited (calc.py: exit 0, no output, proves nothing) in 36 of 41 runs instead of main.py, which imports it."""
    if os.sep in target or (os.altsep and os.altsep in target):
        return []
    files = sorted(p.name for p in SANDBOX.glob("*.py") if p.is_file() and not any(c.isspace() for c in p.name)
                   and p.resolve() not in PROTECTED)[:100]
    imps = {f: _local_imports(f) for f in files}
    users = [f for f in files if f != target and target in imps[f]]
    return [f for f in users if not any(f in imps[g] for g in files if g != f)]


def _ignored_tail(body: str) -> bool:
    """True if the model wrote another CMD: or a DONE: after its command (outside a ``` block or a
    SEARCH/REPLACE block). Only the first command is run, so the rest is thrown away; the model
    should be told, or it believes that its invented follow-up steps happened."""
    in_fence = in_patch = False
    for line in (body or "").splitlines():
        t = line.strip()
        if t.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and t.startswith("<<<<<<<"):
            in_patch = True
        elif not in_fence and t.startswith(">>>>>>>"):
            in_patch = False
        elif not in_fence and not in_patch and t.startswith(("CMD:", "DONE:")):
            return True
    return False


def _clean_reply(reply: str, kind: str, text: str, notes: list) -> str:
    """What goes into history. For an ordinary command keep only NOTE + CMD, so text the
    model invented after the command (fake tool output, a fake DONE) is not remembered."""
    if kind == "cmd" and text.split(" ", 1)[0] not in BLOCK_CMDS:
        return (f"NOTE: {notes[0]}\n" if notes else "") + f"CMD: {text}"
    return _cap_notes(_cut_after_block(reply, text))


def _cut_after_block(reply: str, text: str) -> str:
    """For write_file / patch_file / replace_def keep the reply only up to the end of the command's
    block. Whatever the model wrote after it (an invented 'NOTE: it is fixed', fake tool output, a second
    command, a fake DONE) never happened; remembering it made a model believe its own invention (bugfix
    runs 2 and 3: 'median is now correctly handled' was never true)."""
    lines = reply.splitlines()
    ci = next((i for i, l in enumerate(lines) if l.strip().startswith("CMD:")), None)
    if ci is None:
        return reply
    rest = lines[ci + 1:]
    if text.split(" ", 1)[0] == "patch_file":
        e = next((i for i, l in enumerate(rest) if l.strip().startswith(">>>>>>>")), None)
        end = None if e is None else ci + 1 + e
    else:
        try:
            b = _fence_bounds(rest)
        except Refused:
            return reply
        end = None if b is None else ci + 1 + b[1]
    return reply if end is None else "\n".join(lines[:end + 1])


def _norm(path_str: str) -> str:
    return os.path.normpath(path_str)


def _dump_transcript(task: str, result: str, msgs: list) -> None:
    """If AGENT_TRANSCRIPT=<file> is set, save the whole conversation there as JSON (for the
    testkit's reports and training export). Never raises; the file is written by this
    process, not by the model, and is not inside the sandbox unless you point it there."""
    path = os.environ.get("AGENT_TRANSCRIPT")
    if not path:
        return
    try:
        Path(path).write_text(json.dumps({
            "task": task, "result": result, "stopped": result.startswith("(stopped:"),
            "messages": [{"role": r, "content": c} for r, c in msgs]}, indent=1), encoding="utf-8")
    except Exception:
        pass


def _out_lines(run_result: str) -> list:
    """The printed lines of a run_python result: the '[exit N]' header and '(no output)' removed, trailing spaces
    and blank lines ignored. Used to compare a run with the expected output."""
    body = run_result.split("\n", 1)[1] if "\n" in run_result else ""
    return [l.rstrip() for l in body.splitlines() if l.strip() and l.strip() != "(no output)"]


def _and_list(items: list) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _expect_diff(got: list, expect: list) -> str:
    """Why a run's printed lines differ from the expected lines, for the DONE refusal (W6).
    When the first wrong line has the same number of words as the expected one, each wrong word is named: by its
    name for 'name=value' words with the same name ('orders is 4 but must be 3'), else by position ('word 2 is ...'),
    and the names that are already right are listed. Otherwise (missing line, other word count, one-word line) the
    whole line is shown, as in update 25; so is a line where every word is wrong. The text always contains 'different from the expected output' (the bench
    counts refusals by that phrase)."""
    i = next(k for k in range(max(len(got), len(expect)) + 1)
             if k >= len(got) or k >= len(expect) or got[k] != expect[k])
    head = "the last run printed something different from the expected output"
    g_words = got[i].split() if i < len(got) else []
    e_words = expect[i].split() if i < len(expect) else []
    if (len(g_words) >= 2 and len(g_words) == len(e_words)
            and any(gw == ew for gw, ew in zip(g_words, e_words))):      # all words wrong: the whole line is clearer
        wrong, right, positional = [], [], False
        for k, (gw, ew) in enumerate(zip(g_words, e_words)):
            gn, _, gv = gw.partition("=")
            en, _, ev = ew.partition("=")
            named = "=" in gw and "=" in ew and gn == en and gn != ""
            if gw == ew:
                if named:
                    right.append(gn)
                continue
            if named:
                wrong.append(f"{gn} is {gv or '(empty)'} but must be {ev or '(empty)'}")
            else:
                positional = True
                wrong.append(f"word {k + 1} is '{gw}' but must be '{ew}'")
        if len(wrong) > 6:
            wrong = wrong[:6] + [f"{len(wrong) - 6} more words differ"]
        note = ""
        if right and not positional:
            note = f" ({_and_list(right)} {'is' if len(right) == 1 else 'are'} already right)"
        elif len(wrong) < len(g_words):
            note = " (the other words are already right)"
        which = "that value" if len(wrong) == 1 else "those values"
        return (f"{head}. In line {i + 1}: {'; '.join(wrong)}{note}. Find which file makes {which} wrong, fix it, "
                f"and run the program again")
    g = got[i] if i < len(got) else "(nothing)"
    e = expect[i] if i < len(expect) else "(nothing)"
    return (f"{head}: line {i + 1} is '{g}' but must be '{e}'. Find which file makes that value wrong, fix it, and "
            f"run the program again")


def _load_expect():
    """Expected output for the DONE gate (update 25), from the file named by AGENT_EXPECT_FILE, or None.
    The file is read by the tool, outside the sandbox; the model cannot change it."""
    path = os.environ.get("AGENT_EXPECT_FILE", "")
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return [l.rstrip() for l in f.read().splitlines() if l.strip()]


def _test_file():
    """W10: the task's test file (AGENT_TEST_FILE, a path inside the sandbox), or "" when the task has none."""
    name = os.environ.get("AGENT_TEST_FILE", "").strip()
    if not name:
        return ""
    _check_path(name)
    return os.path.normpath(name).replace(os.sep, "/")


def _code_sig(skip: str) -> str:
    """A fingerprint of every .py file in the sandbox except `skip`: changes when any code changes, by any route."""
    h = hashlib.sha256()
    for f in sorted(SANDBOX.rglob("*.py")):
        rel = f.relative_to(SANDBOX).as_posix()
        if rel == skip or "__pycache__" in f.parts or any(x.startswith(".") for x in f.relative_to(SANDBOX).parts):
            continue
        try:
            h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
        except OSError:
            pass
    return h.hexdigest()


def agent(task: str, max_steps: int = 10) -> str:
    msgs = [("system", SYSTEM), ("user", task)]
    result = "(stopped: crashed)"
    try:
        result = _agent_loop(task, max_steps, msgs)
        return result
    finally:
        global _VIEWS
        _VIEWS = None    # W9: view tracking only inside an agent run (a command typed by hand is not tracked)
        READ_ONLY.clear()  # W10: the test file is read-only only while the run that uses it lasts
        _dump_transcript(task, result, msgs)


# Refusals that mean "the file already has this" (W4: win-W3-omni2 run 1 got a correct DONE refused because a duplicate
# edit was counted as "REFUSED and never applied"). Not in this list on purpose: "...identical, and code was found
# AFTER the >>>>>>> REPLACE line": there the intended code was NOT applied.
_NOOP_REFUSALS = ("REFUSED: this exact edit was already applied",
                  "REFUSED: that is the same code the file already has",
                  "REFUSED: SEARCH and REPLACE are identical, so nothing would change",
                  "REFUSED: those lines already have exactly that text")


def _agent_loop(task: str, max_steps: int, msgs: list) -> str:
    global _VIEWS
    _VIEWS = {}          # W9: numbered lines the model has seen (replace_lines only edits those)
    consecutive_failures = 0
    last_key = None
    done_ok = set()      # write/patch commands that already succeeded
    # Verification state (the DONE gate). A claim of success needs evidence from run_python.
    ran_any = False      # has any command actually run yet (a refused one does not count)
    seen_files = set()   # files the model has actually looked at, written or patched this run
    unverified = set()   # .py files changed since they were last run
    last_exit = {}       # .py file -> exit code of its most recent run
    refused_edit = {}    # file -> command that was REFUSED and not yet redone successfully
    problems = set()     # .py files whose last static check listed problems
    done_blocked = 0
    last_run = None      # the most recent run_python result (the tool's run of main.py counts), for the expect gate
    try:
        expect = _load_expect()
    except (OSError, UnicodeDecodeError) as e:
        return f"(stopped: ERROR: cannot read the expected-output file AGENT_EXPECT_FILE: {e})"
    if expect is not None:
        print(f"[expect] DONE needs a run that prints exactly {len(expect)} expected line(s)")
    try:
        test_name = _test_file()
    except Refused as e:
        return f"(stopped: ERROR: AGENT_TEST_FILE: {e})"
    if test_name and not (SANDBOX / test_name).is_file():
        print(f"[test] AGENT_TEST_FILE {test_name} is not in the sandbox: the test gate is off for this run")
        test_name = ""
    test_ok_sig = None   # W10: code fingerprint at the last PASSING run of the test file (None = not passed)
    if test_name:
        READ_ONLY.add(Path(os.path.realpath(SANDBOX / test_name)))
        print(f"[test] DONE needs a passing run of {test_name} after the last code change ({test_name} is read-only)")

    def record_run(t, res):
        """Gate bookkeeping for one run_python of t (the model's own run, or the tool's run of an entry point)."""
        code = int(res.split("]", 1)[0].split()[1]) if res.split("]", 1)[0].split()[1:] else 1
        seen = {t} | _local_imports(t)
        unverified.difference_update(seen)
        last_exit[t] = code
        nonlocal test_ok_sig
        if test_name and t == test_name:
            test_ok_sig = _code_sig(test_name) if code == 0 else None
        if code == 0:
            for f in seen:  # a clean run of main.py also exercised the helpers it imports
                last_exit[f] = 0 if f in last_exit or f != t else last_exit.get(f, 0)

    for step in range(max_steps):
        global LAST_STATS
        LAST_STATS = ""
        reply = ask_model(msgs)
        if reply.startswith("ERROR: Failed to connect"):
            return f"(stopped: {reply})"
        if LAST_STATS:
            print(f"[llm] {LAST_STATS}")
        kind, text, body, notes = parse_reply(reply)
        msgs.append(("assistant", _clean_reply(reply, kind, text, notes)))
        print(f"model> {reply}")

        if kind == "done":
            why = []
            if REQUIRE_VERIFY and RUN_PYTHON:
                if unverified:
                    why.append("you changed " + ", ".join(sorted(unverified)) +
                               " and have not run it since: use run_python on it first")
                bad = sorted(f for f, c in last_exit.items() if c != 0 and f not in unverified and f != test_name)
                if bad:
                    why.append("the last run of " + ", ".join(bad) + " failed; fix it with replace_def "
                               "or patch_file and run_python again")
                if expect is not None and not why:
                    if last_run is None:
                        why.append("nothing has been run yet, so the output cannot be checked against the expected "
                                   "output: run the program with run_python")
                    elif _out_lines(last_run) != expect:
                        why.append(_expect_diff(_out_lines(last_run), expect))
                if test_name and not unverified:
                    if test_ok_sig is None:
                        why.append(f"the task's tests in {test_name} have not passed yet (not run, or the last run "
                                   f"failed): run_python {test_name}. Each FAIL line names a rule the code breaks: "
                                   f"fix the code, never the test")
                    elif test_ok_sig != _code_sig(test_name):
                        why.append(f"the code changed after the task's tests in {test_name} last passed: "
                                   f"run_python {test_name} again")
            if REQUIRE_WORK_BEFORE_DONE and not ran_any:
                why.append("you have not run any command yet, so nothing has been done. Start with "
                           "read_file or write_file")
            if refused_edit:
                why.append("these were REFUSED and never applied: " +
                           "; ".join(f"{c}" for c in refused_edit.values()) +
                           " (send them again, corrected)")
            if why:
                done_blocked += 1
                msg = "REFUSED: cannot accept DONE yet: " + "; ".join(why) + "."
                print(f"tool>  {msg}")
                if done_blocked >= 3:
                    return "(stopped: DONE was blocked 3 times; " + "; ".join(why) + ")"
                msgs.append(("user", msg))
                continue
            return text  # already shown above as the model> line

        if kind != "cmd":
            hint = "Reply with 'CMD: <command>' or 'DONE: <answer>'."
            if "```" in reply:
                hint += (" Code in a chat reply is not saved. To save it, reply with the line "
                         "'CMD: write_file <name.py>' and put the code in a ``` block on the lines after it.")
            msgs.append(("user", hint))
            continue

        segs, done_ignored = split_commands(reply)
        if not segs:                      # safety net: parse_reply found a command that the splitter did not
            segs = [(text, body, "")]
        outs, last_nudge = [], ""
        reply_views = set()   # W9: files shown or changed earlier in THIS reply (the model wrote its numbers before)
        for n_run, (cmd, body, raw) in enumerate(segs[:MAX_BATCH]):
            if n_run:                     # a further command from the same reply: history shows it as sent
                msgs[-1] = ("assistant", msgs[-1][1] + "\n" + raw)
            key = (cmd, body)  # same file rewritten with new content is not a repeat
            if key == last_key:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
                last_key = key

            words = cmd.split()
            name = words[0] if words else ""
            target = _norm(words[1]) if len(words) > 1 else ""
            is_block = name in BLOCK_CMDS
            is_py = target.endswith(".py")

            pre = ""   # shown to the model before the result; never part of the result itself
            unread = (REQUIRE_READ_BEFORE_PATCH and name in EDIT_CMDS and target and target not in seen_files
                      and (SANDBOX / target).is_file())
            if unread:
                shown = run(f"read_file {target}")
                seen_files.add(target)   # the model is about to see it, so the next edit may go through
                if name == "replace_def":
                    # replace_def swaps one whole function chosen BY NAME, so it is not a guess about line
                    # contents. Do not make the model resend it (zip17: 9 of 10 first replies refused, then
                    # the model re-sent only its first edit and looped). Read the file for it and carry on.
                    pre = (f"(You had not looked at {target} yet, so the tool read it for you. Its content "
                           f"BEFORE your edit:)\n{shown}\n(Your edit follows.)\n")
                    unread = False
            if unread:
                result = (f"REFUSED: you have not looked at {target} yet, so this edit was a guess about what is "
                          f"in it and was NOT applied. The file's real content is below. Send ONE edit again, "
                          f"based on these lines:\n{shown}")
            elif name == "replace_lines" and target in reply_views:
                result = (f"REFUSED: replace_lines on {target} was NOT applied: an earlier command in this same "
                          f"reply showed or changed {target}, so you wrote these line numbers before seeing its "
                          f"current lines. Send replace_lines again in your next reply, using the numbers shown "
                          f"in the output above.")
            elif (REQUIRE_RUN_BEFORE_PATCH and RUN_PYTHON and name in EDIT_CMDS and is_py
                    and target in unverified and target not in problems):
                result = (f"REFUSED: run_python {target} first and read what it prints, then patch it. "
                          f"(A patch has to answer a real error or check problem.)")
            elif is_block and key in done_ok:  # don't redo a finished write/patch
                result = ("REFUSED: this exact edit was already applied earlier in this run, so sending it again "
                          "changes nothing. Do not repeat it. Run the file (run_python) or read_file it to see its "
                          "current state, then fix a DIFFERENT problem. Reply DONE only if the output is right.")
            else:
                result = run(cmd, body)
                if is_block and result.startswith("[exit 0]") and "problem(s) found" not in result:
                    done_ok.add(key)
            print(f"tool>  {_preview(pre + result)}")
            if not result.startswith(("REFUSED:", "ERROR:")):
                ran_any = True

            ok = result.startswith("[exit 0]")
            if target and (pre or name == "read_file" or (is_block and ok) or (name == "replace_lines")):
                reply_views.add(target)   # shown or changed now: numbers written later in this reply are stale
            if not result.startswith(("REFUSED:", "ERROR:")):  # the model has now really seen (or made) these files
                if is_block and ok and target:
                    seen_files.add(target)
                elif name in ("read_file", "cat", "head", "tail", "sed", "grep", "wc", "check_file", "run_python"):
                    seen_files.update(_norm(w) for w in words[1:] if not w.startswith("-"))
            if is_block and target:
                if ok:
                    refused_edit.pop(target, None)
                    if is_py:
                        unverified.add(target)
                        last_exit.pop(target, None)
                        (problems.add if "problem(s) found" in result else problems.discard)(target)
                elif result.startswith(("REFUSED:", "ERROR:")) and not result.startswith(_NOOP_REFUSALS):
                    refused_edit[target] = f"{name} {target}"   # a no-op (the file already has it) is not pending
            elif name == "run_python" and is_py and result.startswith("[exit"):
                record_run(target, result)

            # update 24: running a module that only defines code proves nothing; run the file that imports it
            redirect, judged, auto_top = "", result, ""
            if name == "run_python" and is_py and RUN_PYTHON and ok and result.rstrip().endswith("(no output)"):
                tops = _entry_points(target)
                if len(tops) == 1:
                    top = auto_top = tops[0]
                    judged = run(f"run_python {top}")
                    redirect = (f"\n({target} only defines code, so running it printed nothing and proves nothing. "
                                f"{top} imports it, so the tool ran {top} for you:)\n{judged}")
                    print(f"tool>  (the tool also ran {top}, which imports {target})\n{_preview(judged)}")
                    if judged.startswith("[exit"):
                        record_run(top, judged)
                        seen_files.add(top)
                elif len(tops) > 1:
                    redirect = (f"\n({target} only defines code, so running it proves nothing. Run a file that "
                                f"uses it: {', '.join(tops[:5])}.)")
            if (name == "run_python" and is_py and judged.startswith("[exit")
                    and not (test_name and (auto_top or target) == test_name)):
                last_run = judged   # update 25: what the program printed last (main.py's run when the tool ran it);
                                    # W10.1: a run of the TEST file is not the program's output (the gates must agree)

            if result.startswith(("REFUSED:", "ERROR:")):
                consecutive_failures += 1

            if consecutive_failures >= 3:
                return "(stopped: aborted due to consecutive failures)"

            # Nudge once per missed note; notes are what survives context pruning.
            nudge = "" if notes else "\n(Reminder: start your next reply with a one-line 'NOTE: <finding>'.)"
            if is_block and ok:
                if "problem(s) found" in result:
                    nudge += "\n(Saved, but the check found problems. Fix them with replace_def or patch_file, then run_python it.)"
                elif RUN_PYTHON and is_py:
                    nudge += f"\n(Saved. Verify it now: run_python {words[1]}. Do not repeat this command.)"
                else:
                    nudge += "\n(Saved. If the task is finished, reply: DONE: <answer>. Do not repeat this command.)"
            elif is_block and result.startswith("REFUSED:"):
                nudge += "\n(Nothing was changed. Fix the problem named above and send the command again.)"
            elif name == "run_python" and auto_top:     # judge the step by the entry point's real run
                if not judged.startswith("[exit"):
                    pass
                elif judged.startswith("[exit 0]") and judged.rstrip().endswith("(no output)"):
                    nudge += (f"\n({auto_top} also printed nothing. If the task expects printed output, something "
                              f"is missing: read_file it. Do not reply DONE yet.)")
                elif judged.startswith("[exit 0]"):
                    nudge += "\n(It ran OK. If the output matches the task, reply DONE and say what you saw.)"
                else:
                    nudge += "\n(It failed. Read the error, fix the file with replace_def or patch_file, then run_python again.)"
            elif name == "run_python" and redirect:
                pass
            elif name == "run_python":
                if ok and result.rstrip().endswith("(no output)"):
                    nudge += ("\n(It ran but PRINTED NOTHING. If the task expects printed output, the file is "
                              "missing code: read_file it and fix it. Do not reply DONE yet.)")
                elif ok:
                    nudge += "\n(It ran OK. If the output matches the task, reply DONE and say what you saw.)"
                elif result.startswith("[exit"):
                    nudge += "\n(It failed. Read the error, fix the file with replace_def or patch_file, then run_python again.)"
            outs.append((cmd, pre + result + redirect))
            last_nudge = nudge
            good = not result.startswith(("REFUSED:", "ERROR:")) and (
                not result.startswith("[exit") or result.startswith("[exit 0]"))
            if not good or name == "run_python":   # a failure, or a run whose output decides the next step
                break
        not_run = [t for t, _, _ in segs[len(outs):]]
        extra = ""
        if not_run:
            shown = "; ".join(not_run[:5])
            extra += (f"\n(NOT RUN: {shown}. Commands run in order and stop at the first problem or at a "
                      f"run_python, and at most {MAX_BATCH} run per reply. Send the ones you still need again, "
                      f"after reading the output above.)")
        if done_ignored:
            extra += ("\n(Your DONE in that reply was ignored: it was written before any output existed. "
                      "Reply DONE on its own after you have seen the real output.)")
        if len(outs) == 1:
            msgs.append(("user", f"Here is the output from running '{outs[0][0]}':\n{outs[0][1]}{last_nudge}{extra}"))
        else:
            body_txt = "\n\n".join(f"[{i}] {c}\n{r}" for i, (c, r) in enumerate(outs, 1))
            msgs.append(("user", f"Here is the output of your {len(outs)} commands, run in order:\n\n"
                                 f"{body_txt}{last_nudge}{extra}"))

    return "(stopped: step limit reached)"


def _warn_if_guard_writable():
    """Remind the user to lock this file if it lives inside the sandbox."""
    me = Path(__file__).resolve()
    try:
        if (SANDBOX == me.parent or SANDBOX in me.parents) and me.stat().st_mode & 0o222:
            print(f"[warn] {me.name} is writable and inside the sandbox. "
                  f"Lock it with: chmod 444 {me.name}", file=sys.stderr)
    except OSError:
        pass


USAGE = """sandbox-tool: run an LLM agent inside a restricted folder.

  sandbox-tool --agent "task text"   run the agent loop on a task
  sandbox-tool --ping                check that the model server answers (and how fast)
  sandbox-tool "ls -la"              run ONE allowlisted command by hand (what the agent can do)
  sandbox-tool --selfcheck           report what works on this machine (Windows, Git Bash, WSL, Termux, Linux)
  sandbox-tool --version

The sandbox folder is $SANDBOX_DIR, or ./sandbox in the current folder if that is not set.
Set BACKEND=gemini (and GEMINI_API_KEY) for Gemini, otherwise a local OpenAI-compatible
server (e.g. llama.cpp's llama-server) at $LLAMA_URL is used. See the README for all settings.
"""


def _sandbox_problem():
    """A reason this sandbox choice is unsafe or unusable, or None."""
    home = Path.home().resolve()
    if SANDBOX == Path(SANDBOX.anchor) or SANDBOX == home or SANDBOX in home.parents:
        return (f"{SANDBOX} is too broad to use as a sandbox (the model could read everything in it, "
                f"for example your keys and config files). Point SANDBOX_DIR at a dedicated folder.")
    return None


def selfcheck() -> int:
    """Report what really works on THIS machine (Windows PowerShell, Git Bash, WSL, Termux, Linux, macOS).
    Runs in a throwaway folder, never in your sandbox. Each line is ok / FAIL / warn / skip:
      FAIL = something that must work is wrong or unsafe here (exit code 1)
      warn = an optional feature is missing (for example no `cat` on Windows); the tool still works
      skip = could not be tested here and why
    Paste the whole output when you ask for help; nothing in it is private except folder names."""
    import locale
    import platform
    import tempfile
    global SANDBOX, AUDIT_LOG, RUN_TIMEOUT_S, RUN_MEM_MB, RUN_MAX_WRITE_MB
    rows = []

    def rec(status, name, detail=""):
        rows.append(status)
        detail = str(detail).replace("\r", " ").replace("\n", " ")   # a Windows "\r" in output scrambled a line (W3)
        line = f"  [{status:4}] {name}" + (f" -> {detail}" if detail else "")
        print(line.encode("ascii", "replace").decode("ascii"))

    def has(res, text):
        return text in res

    print("sandbox-tool selfcheck")
    print(f"  platform   {platform.platform()}  (os.name={os.name})")
    print(f"  python     {sys.version.split()[0]}  {sys.executable}")
    print(f"  encodings  locale={locale.getpreferredencoding(False)} stdout={getattr(sys.stdout, 'encoding', '?')} "
          f"filesystem={sys.getfilesystemencoding()}")
    print(f"  sandbox    {SANDBOX}  (not touched by this check)")
    print(f"  PATH used  {SAFE_PATH or '(empty)'}")
    old = (SANDBOX, AUDIT_LOG)
    tmp = Path(tempfile.mkdtemp(prefix="sbx_selfcheck_")).resolve()
    try:
        SANDBOX = tmp / "box"
        SANDBOX.mkdir()
        AUDIT_LOG = tmp / "audit.log"
        secret = tmp / "outside.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        # 1. text encoding and line endings
        try:
            run("write_file u.txt", "```\ncafe\u0301 \u2713 \u65e5\u672c\n```")
            raw = (SANDBOX / "u.txt").read_bytes()
            rec("ok" if raw == "cafe\u0301 \u2713 \u65e5\u672c\n".encode("utf-8") else "FAIL", "write_file stores UTF-8 with LF line ends", repr(raw[:30]))
            back = run("read_file u.txt")
            rec("ok" if has(back, "cafe\u0301 \u2713") else "FAIL", "read_file returns the same text")
            (SANDBOX / "c.py").write_bytes(b"a = 1\r\nb = 2\r\n")
            run("patch_file c.py", "<<<<<<< SEARCH\nb = 2\n=======\nb = 3\n>>>>>>> REPLACE")
            raw = (SANDBOX / "c.py").read_bytes()
            rec("ok" if raw == b"a = 1\r\nb = 3\r\n" else "FAIL", "patch_file keeps a CRLF file as CRLF", repr(raw))
            (SANDBOX / "d.py").write_bytes(b"def f():\r\n    return 1\r\n")
            run("replace_def d.py f", "```python\ndef f():\n    return 2\n```")
            raw = (SANDBOX / "d.py").read_bytes()
            rec("ok" if raw == b"def f():\r\n    return 2\r\n" else "FAIL", "replace_def keeps a CRLF file as CRLF", repr(raw))
        except Exception as e:  # a crash here is a finding, not something to hide
            rec("FAIL", "text encoding / line endings", f"{type(e).__name__}: {e}")
        # 2. nothing outside the sandbox can be read
        for label, path in (("../outside.txt", "../outside.txt"), ("..\\outside.txt", "..\\outside.txt"),
                            ("absolute path", str(secret)), ("./../outside.txt", "./../outside.txt")):
            try:
                res = run(f"read_file {shlex.quote(path)}")
                rec("FAIL" if "TOPSECRET" in res else "ok", f"read_file {label} does not leak", res.splitlines()[0][:60] if res else "")
            except Exception as e:
                rec("FAIL", f"read_file {label}", f"{type(e).__name__}: {e}")
        try:
            os.symlink(secret, SANDBOX / "link.txt")
        except (OSError, NotImplementedError, AttributeError) as e:
            rec("skip", "symlink escape test", f"cannot create a symlink here ({type(e).__name__}); fine, the model cannot either")
        else:
            res = run("read_file link.txt")
            rec("FAIL" if "TOPSECRET" in res else "ok", "a symlink pointing outside is not followed", res.splitlines()[0][:60])
        # 3. the allowlisted programs
        for c in ("cat", "ls", "head", "tail", "wc", "grep", "sed"):
            where = _find_exe(c)
            if where:
                rec("ok", f"{c} found", where)
            else:
                rec("warn", f"{c} NOT found", "the model gets 'not installed' and uses read_file instead")
        if _find_exe("cat"):
            res = run("cat u.txt")
            rec("ok" if "cafe\u0301 \u2713" in res and res.startswith("[exit 0]") else "FAIL", "cat prints UTF-8 text correctly", res[:40].replace("\n", " "))
        if _find_exe("sed"):
            res = run("sed -n 1p u.txt")
            rec("ok" if res.startswith("[exit 0]") else "warn", "sed accepts the --sandbox option the tool adds (GNU sed)", res[:60].replace("\n", " "))
        # 4. run_python
        if not RUN_PYTHON:
            rec("skip", "run_python", "switched off (RUN_PYTHON=0, or this Python has neither POSIX limits nor Windows Job Objects)")
        else:
            if _has_posix_limits():
                rec("ok", "run_python limits: POSIX (resource limits, process group, /proc memory watch)")
                rec("ok" if _rss_mb(os.getpid()) is not None else "warn", "memory watchdog can read /proc", "" if _rss_mb(os.getpid()) is not None else "run_python will not enforce RUN_MEM_MB")
            else:
                rec("ok", "run_python limits: Windows Job Object (memory cap, one process, kill on close, CPU backstop)",
                    f"python started: {_job_python()}")
            (SANDBOX / "hi.py").write_text("print('hi \u00e9')", encoding="utf-8")
            res = run("run_python hi.py")
            rec("ok" if res.startswith("[exit 0]") and "hi" in res else "FAIL", "run_python runs a script", res[:50].replace("\n", " "))
            (SANDBOX / "esc.py").write_text(f"print(open({str(secret)!r}).read())", encoding="utf-8")
            res = run("run_python esc.py")
            rec("FAIL" if "TOPSECRET" in res else "ok", "a script cannot read a file outside the sandbox", res[:70].replace("\n", " "))
            # the limits themselves (W2: so a Windows run proves the Job Object really holds), with short test limits
            lim = (RUN_TIMEOUT_S, RUN_MEM_MB, RUN_MAX_WRITE_MB)
            try:
                RUN_TIMEOUT_S, RUN_MEM_MB, RUN_MAX_WRITE_MB = 3, 200, 2
                (SANDBOX / "loop.py").write_text("while True:\n    pass\n", encoding="utf-8")
                t0 = time.monotonic()
                res = run("run_python loop.py")
                dt = time.monotonic() - t0
                rec("ok" if res.startswith("[exit 124]") and dt < 8 else "FAIL", "an infinite loop is stopped by the time limit (3 s here)", f"{dt:.1f}s " + res.splitlines()[0])
                (SANDBOX / "mem.py").write_text("x = []\nwhile len(x) < 60:\n    x.append(bytearray(10**7))\nprint('allocated 600 MB')\n", encoding="utf-8")
                res = run("run_python mem.py")
                rec("ok" if res.startswith("[exit 137]") else "FAIL", "a script that keeps allocating is stopped at the memory limit (200 MB here)", res.splitlines()[0])
                (SANDBOX / "spawn.py").write_text("import sys\nr = __import__('sub' + 'process').run([sys.executable, '-c', 'print(6 * 7)'], "
                                                  "capture_output=True, text=True)\nprint('GOT', r.stdout.strip())\n", encoding="utf-8")
                res = run("run_python spawn.py")
                rec("FAIL" if "GOT 42" in res else "ok", "a script cannot start another program", res.splitlines()[-1][:70])
                (SANDBOX / "flood.py").write_text("while True:\n    print('x' * 1000)\n", encoding="utf-8")
                res = run("run_python flood.py")
                rec("ok" if "limit" in res.splitlines()[0] and len(res) < 12000 else "FAIL", "endless output is stopped and capped", res.splitlines()[0][:70])
            finally:
                RUN_TIMEOUT_S, RUN_MEM_MB, RUN_MAX_WRITE_MB = lim
    finally:
        SANDBOX, AUDIT_LOG = old
        shutil.rmtree(tmp, ignore_errors=True)
    bad = rows.count("FAIL")
    print(f"\n  {rows.count('ok')} ok, {bad} FAIL, {rows.count('warn')} warn, {rows.count('skip')} skip")
    print("  Nothing here proves the model will behave; it shows which parts of the TOOL work on this machine.")
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "--version":
        print(f"sandbox-tool {__version__}")
        return 0
    for _st in (sys.stdout, sys.stderr):   # a Windows console or a redirected pipe may not be UTF-8:
        try:                                # print "?" for a character it cannot show instead of crashing
            _st.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if argv == ["--selfcheck"]:
        return selfcheck()
    problem = _sandbox_problem()
    if problem:
        print("STOP:", problem, file=sys.stderr)
        return 2
    if not SANDBOX.is_dir():
        if "SANDBOX_DIR" in os.environ:
            print(f"STOP: SANDBOX_DIR={os.environ['SANDBOX_DIR']} is not an existing folder. Create it first.", file=sys.stderr)
            return 2
        SANDBOX.mkdir(parents=True)          # the default ./sandbox
        print(f"[info] created sandbox folder {SANDBOX}", file=sys.stderr)
    if len(argv) >= 2 and argv[0] == "--agent":
        _warn_if_guard_writable()
        result = agent(" ".join(argv[1:]), int(os.environ.get("AGENT_MAX_STEPS") or 10))
        if result.startswith("(stopped:"):  # a DONE answer was already printed as model>
            print("final>", result)
    elif argv == ["--ping"]:
        print(ping())
    else:
        print(run(" ".join(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
