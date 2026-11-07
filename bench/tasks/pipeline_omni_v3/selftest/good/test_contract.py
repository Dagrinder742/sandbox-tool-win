"""The rules ingest.py and calc.py must follow, written as tests. Run it: python test_contract.py
Each line says PASS or FAIL and the rule. Do not change this file: fix the code it tests."""
from ingest import DataIngester
import calc

ROWS = [
    {"item": "tea", "price": "2.00", "qty": 3},
    {"item": "", "price": "4.00", "qty": 1},
    {"item": "cup", "price": "6.50", "qty": 0},
    {"item": "jam", "price": "0.75", "qty": -2},
    {"item": "pie", "price": "9", "qty": 1},
]
results = []


def test(rule, fn):
    try:
        fn()
        results.append(True)
        print(f"PASS {rule}")
    except Exception as e:
        results.append(False)
        print(f"FAIL {rule}: {type(e).__name__}: {e}")


def keeps_only_valid_rows():
    got = [r["item"] for r in DataIngester(ROWS).clean()]
    assert got == ["tea", "pie"], f"kept {got}, expected ['tea', 'pie']"


def prices_are_floats():
    for r in DataIngester(ROWS).clean():
        assert type(r["price"]) is float, f"price of {r['item']!r} is {type(r['price']).__name__} {r['price']!r}"


def no_rows_gives_no_rows():
    assert DataIngester([]).clean() == [], "clean() of no rows must be an empty list"


def subtotal_is_price_times_qty():
    got = calc.subtotal([{"item": "x", "price": 2.5, "qty": 4}])
    assert got == 10.0, f"subtotal is {got!r}, expected 10.0"


def average_of_no_rows_is_zero():
    got = calc.average_order([])
    assert got == 0.0, f"average_order([]) is {got!r}, expected 0.0"


test("ingest.py: clean() keeps only rows with a non-empty item and qty > 0", keeps_only_valid_rows)
test("ingest.py: clean() turns every kept price into a float", prices_are_floats)
test("ingest.py: clean() of no rows is an empty list", no_rows_gives_no_rows)
test("calc.py: subtotal() is the sum of price * qty", subtotal_is_price_times_qty)
test("calc.py: average_order() of no rows is 0.0", average_of_no_rows_is_zero)

print(f"{sum(results)} of {len(results)} tests passed")
raise SystemExit(0 if all(results) else 1)
