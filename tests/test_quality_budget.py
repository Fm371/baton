import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("quality_budget", ROOT / "tools" / "quality_budget.py")
qb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qb)


def branchy(n):
    """A function with n `if` branches (complexity n + 1)."""
    return "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(n)) + "    return -1\n"


def long_func(lines):
    return "def g():\n" + "    x = 1\n" * (lines - 1)


class MeasureTest(unittest.TestCase):
    def measure(self, src):
        return qb.measure_source(src, "baton/m.py")

    def test_counts_decisions(self):
        m = self.measure("def f(a, b):\n"
                         "    if a and b:\n"            # if +1, and +1
                         "        return 1\n"
                         "    for i in a:\n"             # +1
                         "        pass\n"
                         "    try:\n"
                         "        pass\n"
                         "    except ValueError:\n"      # +1
                         "        pass\n"
                         "    return [x for x in a if x]\n")   # comprehension +1, its condition +1
        self.assertEqual(m["baton/m.py::f"]["cc"], 1 + 1 + 1 + 1 + 1 + 2)

    def test_nested_function_is_counted_on_its_own(self):
        m = self.measure("def outer():\n    def inner(x):\n        if x:\n            return 1\n    return inner\n")
        self.assertEqual(m["baton/m.py::outer"]["cc"], 1)
        self.assertEqual(m["baton/m.py::outer.inner"]["cc"], 2)

    def test_methods_are_named_with_their_class(self):
        m = self.measure("class A:\n    def run(self):\n        return 1\n")
        self.assertIn("baton/m.py::A.run", m)

    def test_length_in_lines(self):
        self.assertEqual(self.measure(long_func(30))["baton/m.py::g"]["lines"], 30)


class CheckTest(unittest.TestCase):
    def measured(self, src):
        return qb.measure_source(src, "baton/m.py")

    def test_small_code_passes(self):
        self.assertEqual(qb.check(self.measured(branchy(3)), {}), [])

    def test_new_complex_function_fails(self):
        problems = qb.check(self.measured(branchy(20)), {})
        self.assertEqual(len(problems), 1)
        self.assertIn("new function over the budget", problems[0])

    def test_new_long_function_fails(self):
        self.assertEqual(len(qb.check(self.measured(long_func(100)), {})), 1)

    def test_a_baselined_offender_is_tolerated_at_its_value(self):
        m = self.measured(branchy(20))
        self.assertEqual(qb.check(m, {"baton/m.py::f": dict(m["baton/m.py::f"])}), [])

    def test_a_baselined_offender_that_got_worse_fails(self):
        m = self.measured(branchy(22))
        base = {"baton/m.py::f": {"cc": 21, "lines": m["baton/m.py::f"]["lines"]}}
        problems = qb.check(m, base)
        self.assertEqual(len(problems), 1)
        self.assertIn("got worse", problems[0])

    def test_improvement_is_reported_not_failed(self):
        m = self.measured(branchy(3))
        base = {"baton/m.py::f": {"cc": 30, "lines": 90}}
        self.assertEqual(qb.check(m, base), [])
        self.assertEqual(len(qb.improvements(m, base)), 1)


class UpdateTest(unittest.TestCase):
    def test_update_refuses_a_rise_without_force(self):
        m = qb.measure_source(branchy(22), "baton/m.py")
        base = {"baton/m.py::f": {"cc": 21, "lines": m["baton/m.py::f"]["lines"]}}
        with self.assertRaises(ValueError):
            qb.updated_baseline(m, base, qb.MAX_CC, qb.MAX_LINES, force=False)
        out = qb.updated_baseline(m, base, qb.MAX_CC, qb.MAX_LINES, force=True)
        self.assertEqual(out["baton/m.py::f"]["cc"], 23)

    def test_update_lowers_and_drops(self):
        m = qb.measure_source(branchy(3), "baton/m.py")
        base = {"baton/m.py::f": {"cc": 30, "lines": 90}, "baton/gone.py::h": {"cc": 20, "lines": 10}}
        self.assertEqual(qb.updated_baseline(m, base, qb.MAX_CC, qb.MAX_LINES, force=False), {})


def quiet_main(args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return qb.main(args)


class CommandLineTest(unittest.TestCase):
    def tree(self, src):
        root = Path(tempfile.mkdtemp())
        (root / "baton").mkdir()
        (root / "baton" / "m.py").write_text(src)
        return root

    def test_first_update_then_check_then_worse(self):
        root = self.tree(branchy(20))
        baseline = root / "baseline.json"
        args = ["--root", str(root), "--baseline", str(baseline)]
        self.assertEqual(quiet_main(args), 1)                          # no baseline: a new offender
        self.assertEqual(quiet_main([*args, "--update"]), 0)           # first time: recorded as is
        self.assertEqual(json.loads(baseline.read_text())["functions"]["baton/m.py::f"]["cc"], 21)
        self.assertEqual(quiet_main(args), 0)
        (root / "baton" / "m.py").write_text(branchy(25))
        self.assertEqual(quiet_main(args), 1)                          # got worse
        self.assertEqual(quiet_main([*args, "--update"]), 1)           # refuses to raise it
        self.assertEqual(quiet_main([*args, "--update", "--force"]), 0)

    def test_the_repository_itself_is_within_its_budget(self):
        self.assertEqual(quiet_main([]), 0)


if __name__ == "__main__":
    unittest.main()
