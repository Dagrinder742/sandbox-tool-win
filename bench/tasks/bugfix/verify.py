import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vkit import header

c = header(sys.argv)
m = c.load()


def eq(got, want):
    assert got == want, f"got {got!r}, expected {want!r}"


def no_mutation():
    data = [3, 1, 2]
    m.median(data)
    eq(data, [3, 1, 2])


c.need_mod("average of numbers", lambda: eq(m.average([1, 2, 3, 4]), 2.5))
c.need_mod("average of an empty list is 0.0", lambda: eq(m.average([]), 0.0))
c.need_mod("median, odd count", lambda: (eq(m.median([3, 1, 2]), 2), eq(m.median([5]), 5), eq(m.median([9, 1, 5, 7, 3]), 5)) and None)
c.need_mod("median, even count is the mean of the middle two", lambda: (eq(m.median([1, 2, 3, 4]), 2.5), eq(m.median([10, 2]), 6.0)) and None)
c.need_mod("median does not change its input", no_mutation)


def script():
    rc, out, err = c.run_script()
    assert rc == 0, f"exit {rc}: {err.strip()[-150:]}"
    eq(out.split(), ["2.5", "2.5", "0.0"])


c.need_mod("running the file prints 2.5, 2.5, 0.0", script)
c.finish()
