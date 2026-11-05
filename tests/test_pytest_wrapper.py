"""Lets `pytest` run the two script-style suites. Each must exit 0 and end with ALL OK."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(script):
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0 and r.stdout.strip().endswith("ALL OK"), (r.stdout + r.stderr)[-3000:]


def test_sandbox_tool_suite():
    _run(ROOT / "tests" / "test_sandbox_tool.py")


def test_bench_suite():
    _run(ROOT / "bench" / "test_bench.py")
