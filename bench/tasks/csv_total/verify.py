import os, random, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vkit import header

c = header(sys.argv)
c.loaded = os.path.isfile(c.path)
if not c.loaded:
    c.results.append((False, "output file exists", f"not found: {c.path}"))


def try_csv(rows, label):
    def go():
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "sales.csv"), "w") as f:
                f.write("item,qty,price\n" + "".join(f"{n},{q},{p}\n" for n, q, p in rows))
            rc, out, err = c.run_script(cwd=d)
            assert rc == 0, f"exit {rc}: {err.strip()[-150:]}"
            want = "Total: %.2f" % sum(q * p for _, q, p in rows)
            assert out.strip() == want, f"printed {out.strip()!r}, expected {want!r}"
    return go


rnd = random.Random(7)
c.need_mod("the original sales.csv gives Total: 6.50", try_csv([("bolts", 10, 0.25), ("nuts", 4, 0.5), ("washers", 100, 0.02)], "orig"))
c.need_mod("different rows give the right total (no hard-coded numbers)", try_csv(
    [("a", 3, 1.5), ("b", 7, 2.25), ("c", 1, 99.99), ("d", 12, 0.1)], "other"))
c.need_mod("many rows", try_csv([(f"i{k}", rnd.randint(1, 50), round(rnd.uniform(0.1, 20), 2)) for k in range(40)], "many"))
c.need_mod("header only prints Total: 0.00", try_csv([], "empty"))
c.need_mod("uses the csv module", lambda: (_ for _ in ()).throw(AssertionError("no 'import csv' / 'from csv'"))
           if "csv" not in open(c.path).read().replace("sales.csv", "") else None)
c.finish()
