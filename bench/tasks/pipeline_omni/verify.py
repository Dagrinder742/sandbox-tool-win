"""Verifier for pipeline_omni. Given the path of main.py; ingest.py and calc.py are taken from the same folder.

Judges the files, never what the model said. The checks use their own data, so hard-coded output cannot pass.
"""
import importlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vkit import header

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED = ["orders=3 subtotal=58.00 tax=4.64 total=62.64 average=19.33",
            "orders=0 subtotal=0.00 tax=0.00 total=0.00 average=0.00"]

c = header(sys.argv)
folder = os.path.dirname(c.path)


def eq(got, want):
    assert got == want, f"got {got!r}, expected {want!r}"


def close(got, want):
    assert isinstance(got, (int, float)) and abs(got - want) < 1e-9, f"got {got!r}, expected {want!r}"


def _text(p):
    with open(p, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n").strip()


def main_unchanged():
    assert os.path.isfile(c.path), "main.py is missing"
    eq(_text(c.path) == _text(os.path.join(HERE, "files", "main.py")), True)


c.check("main.py is unchanged (the bugs had to be fixed where they are)", main_unchanged)

mods = {}


def load(name):
    def go():
        assert os.path.isfile(os.path.join(folder, name + ".py")), f"{name}.py is missing"
        if folder not in sys.path:
            sys.path.insert(0, folder)
        mods[name], _ = c.quiet(importlib.import_module, name)
    return go


c.check("ingest.py imports without error", load("ingest"))
c.check("calc.py imports without error", load("calc"))


def need(name, label, fn):
    if name not in mods:
        c.results.append((False, label, f"skipped: {name}.py does not import"))
    else:
        c.check(label, fn)


def cleaned(rows):
    out, _ = c.quiet(lambda: mods["ingest"].DataIngester(rows).clean())
    return list(out)


ROWS = [{"item": "a", "price": "2.25", "qty": 2}, {"item": "", "price": "1.00", "qty": 1},
        {"item": "b", "price": "5.00", "qty": 0}, {"item": "c", "price": "0.50", "qty": -3},
        {"item": "d", "price": "10", "qty": 1}]


def keeps_valid():
    eq([r["item"] for r in cleaned(ROWS)], ["a", "d"])


def drops_qty_zero():
    eq(cleaned([{"item": "x", "price": "1.00", "qty": 0}]), [])


def prices_float():
    got = cleaned(ROWS)
    assert all(type(r["price"]) is float for r in got), f"price types: {[type(r['price']).__name__ for r in got]}"
    eq([r["price"] for r in got], [2.25, 10.0])


def clean_empty():
    eq(cleaned([]), [])


need("ingest", "clean() drops rows with an empty item or qty <= 0", keeps_valid)
need("ingest", "clean() drops a row with qty 0 (the mug)", drops_qty_zero)
need("ingest", "clean() returns price as a float (the root cause of the TypeError in calc.py)", prices_float)
need("ingest", "clean() of no rows is an empty list", clean_empty)

F = [{"item": "a", "price": 2.5, "qty": 2}, {"item": "b", "price": 1.25, "qty": 4}]


def calc_subtotal():
    close(mods["calc"].subtotal(F), 10.0)


def calc_average():
    close(mods["calc"].average_order(F), 5.0)


def calc_average_empty():
    close(mods["calc"].average_order([]), 0.0)


def calc_summary_empty():
    s = mods["calc"].summary([])
    for k in ("orders", "subtotal", "tax", "total", "average"):
        close(s[k], 0.0)


def calc_summary():
    s = mods["calc"].summary(F)
    eq(s["orders"], 2)
    close(s["subtotal"], 10.0)
    close(s["tax"], 0.8)
    close(s["total"], 10.8)
    close(s["average"], 5.0)


need("calc", "subtotal() is the sum of price * qty", calc_subtotal)
need("calc", "average_order() is subtotal / number of rows", calc_average)
need("calc", "average_order([]) is 0.0 (the ZeroDivisionError)", calc_average_empty)
need("calc", "summary([]) is all zeros without crashing", calc_summary_empty)
need("calc", "summary() of two rows has the right orders, subtotal, tax, total, average", calc_summary)


def script():
    try:
        p = subprocess.run([sys.executable, "-E", "-s", os.path.basename(c.path)], cwd=folder,
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise AssertionError("main.py did not finish within 10s")
    assert p.returncode == 0, f"exit {p.returncode}: {p.stderr.strip()[-160:]}"
    eq([l.strip() for l in p.stdout.strip().splitlines()], EXPECTED)


c.check("running main.py prints exactly the two expected lines", script)
c.finish()
