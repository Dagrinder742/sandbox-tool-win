import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vkit import header

c = header(sys.argv)
m = c.load()


def eq(got, want):
    assert got == want, f"got {got!r}, expected {want!r}"


def raises(fn, msg):
    try:
        fn()
    except IndexError as e:
        assert str(e) == msg, f"IndexError message was {str(e)!r}, expected {msg!r}"
        return
    raise AssertionError("no IndexError was raised")


def lifo():
    s = m.Stack()
    for x in (1, 2, 3):
        s.push(x)
    eq([s.pop(), s.pop(), s.pop()], [3, 2, 1])


def peek():
    s = m.Stack()
    s.push("a"); s.push("b")
    eq(s.peek(), "b"); eq(s.size(), 2); eq(s.peek(), "b")


def empty():
    s = m.Stack()
    eq(s.is_empty(), True); eq(s.size(), 0)
    s.push(0)
    eq(s.is_empty(), False); eq(s.size(), 1)
    s.pop()
    eq(s.is_empty(), True)


def items_attr():
    s = m.Stack()
    s.push(5)
    eq(s.items, [5])


def independent():
    a, b = m.Stack(), m.Stack()
    a.push(1)
    eq(b.size(), 0)


c.need_mod("class Stack exists, starts empty", lambda: eq(m.Stack().size(), 0))
c.need_mod("last in, first out", lifo)
c.need_mod("peek does not remove", peek)
c.need_mod("is_empty and size follow pushes and pops", empty)
c.need_mod("pop on empty raises IndexError with the right message", lambda: raises(lambda: m.Stack().pop(), "pop from empty stack"))
c.need_mod("peek on empty raises IndexError with the right message", lambda: raises(lambda: m.Stack().peek(), "peek from empty stack"))
c.need_mod("items are kept in self.items", items_attr)
c.need_mod("two stacks do not share items", independent)
c.need_mod("nothing printed on import", lambda: eq(c.import_output, ""))


def script():
    rc, out, err = c.run_script()
    assert rc == 0, f"exit {rc}: {err.strip()[-150:]}"
    eq(out.strip(), "size: 2")


c.need_mod("running the file prints 'size: 2'", script)
c.finish()
