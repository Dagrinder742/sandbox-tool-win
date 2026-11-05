import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vkit import header

c = header(sys.argv)
m = c.load()


def eq(got, want):
    assert got == want, f"got {got!r}, expected {want!r}"


c.need_mod("count_words and top_words exist", lambda: (m.count_words, m.top_words) and None)
c.need_mod("lowercases and counts", lambda: eq(m.count_words("Hello hello WORLD"), {"hello": 2, "world": 1}))
c.need_mod("strips punctuation at word ends", lambda: eq(
    m.count_words('Hello, world! "quoted" (paren); done.'), {"hello": 1, "world": 1, "quoted": 1, "paren": 1, "done": 1}))
c.need_mod("keeps inner apostrophes", lambda: eq(m.count_words("it's it's"), {"it's": 2}))
c.need_mod("splits on any whitespace", lambda: eq(m.count_words("a\tb\nc  a"), {"a": 2, "b": 1, "c": 1}))
c.need_mod("empty text and pure punctuation", lambda: (eq(m.count_words(""), {}), eq(m.count_words(" ... ,, "), {})) and None)
c.need_mod("top_words orders by count", lambda: eq(m.top_words("b a b c b a", 2), [("b", 3), ("a", 2)]))
c.need_mod("top_words breaks ties alphabetically", lambda: eq(m.top_words("pear apple fig", 3), [("apple", 1), ("fig", 1), ("pear", 1)]))
c.need_mod("top_words with n larger than distinct words", lambda: eq(m.top_words("x y x", 10), [("x", 2), ("y", 1)]))
c.need_mod("top_words returns tuples in a list", lambda: (
    eq(type(m.top_words("a", 1)), list), eq(type(m.top_words("a", 1)[0]), tuple)) and None)
c.need_mod("nothing printed on import", lambda: eq(c.import_output, ""))


def script():
    rc, out, err = c.run_script()
    assert rc == 0, f"exit {rc}: {err.strip()[-150:]}"
    eq(out.strip(), "[('the', 3), ('and', 2)]")


c.need_mod("running the file prints the demo result", script)
c.finish()
