"""repo-suite: the full suite once per repository and feature, reporting only new failures."""

import unittest

from baton.checks import CheckContext, run_template
from baton.checks.reposuite import failing_tests
from baton.config import ConfigError, load_config
from baton.preflight import plan_text, preflight
from baton.tasks import load_tasks
from tests.test_runner import RunnerFixture

PYTEST = """\
============ short test summary info ============
FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2
FAILED tests/test_b.py::test_two
ERROR tests/test_c.py::test_three - RuntimeError
== 3 failed, 10 passed in 1.2s ==
"""
UNITTEST = """\
FAIL: test_x (pkg.test_mod.Case)
ERROR: test_y (pkg.test_mod.Case)

FAILED (failures=1, errors=1)
"""
GO = "--- FAIL: TestOrders (0.01s)\n--- FAIL: TestOrders/empty (0.00s)\nFAIL\tshop/orders\t0.1s\n"
JEST = "FAIL src/cart.test.ts\n  ✕ adds an item (4 ms)\n  ✓ removes an item\n"


class ParseTest(unittest.TestCase):
    def test_pytest(self):
        self.assertEqual(failing_tests(PYTEST), ["tests/test_a.py::test_one", "tests/test_b.py::test_two",
                                                 "tests/test_c.py::test_three"])

    def test_unittest_does_not_mistake_its_summary_for_a_test(self):
        self.assertEqual(failing_tests(UNITTEST), ["test_x (pkg.test_mod.Case)", "test_y (pkg.test_mod.Case)"])

    def test_go_and_jest(self):
        self.assertEqual(failing_tests(GO), ["TestOrders", "TestOrders/empty"])
        self.assertEqual(failing_tests(JEST), ["src/cart.test.ts", "adds an item"])

    def test_a_custom_regex_replaces_the_defaults(self):
        import re
        self.assertEqual(failing_tests("not ok 3 - login\nok 4 - logout\n", re.compile(r"^not ok \d+ - (.+)$")),
                         ["login"])


class SuiteFixture(RunnerFixture):
    def suite(self, output="", code=0, baseline=None, **repo_opts):
        script = self.run_dir / "suite.sh"
        script.write_text(f"cat <<'EOF'\n{output}EOF\nexit {code}\n")
        opts = {"suite_cmd": f"sh {script}", **repo_opts}
        lines = "".join(f"{k} = {v!r}\n".replace("'", '"') if isinstance(v, str) else f"{k} = {v}\n"
                        for k, v in opts.items())
        if baseline is not None:
            (self.run_dir / "baseline.txt").write_text(baseline)
            lines += 'suite_baseline = "baseline.txt"\n'
        self.write_config(extra=f'[repos."../repo"]\n{lines}')

    def check(self):
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        (self.run_dir / "verifiers" / "suite.md").write_text(
            '+++\nkind = "verify"\ntemplate = "repo-suite"\nrepo = "../repo"\n+++\n')
        cfg = load_config(self.run_dir / "baton.toml")
        task = next(t for t in load_tasks(cfg) if t.id == "suite")
        res = run_template(CheckContext(cfg, task, cfg.results_dir / "suite"))
        return res, res.markdown(task, cfg.results_dir)


class CheckTest(SuiteFixture):
    def test_a_green_suite_passes(self):
        self.suite("12 passed\n")
        res, report = self.check()
        self.assertEqual(res.gaps, [])
        self.assertIn("all passed", report)
        self.assertTrue((self.run_dir / "results" / "suite" / "suite.log").is_file())

    def test_a_green_suite_says_which_baseline_entries_now_pass(self):
        self.suite("12 passed\n", baseline="# known\ntests/test_a.py::test_one\n")
        res, report = self.check()
        self.assertEqual(res.gaps, [])
        self.assertIn("1 baseline failure(s) now pass", report)
        self.assertIn("tests/test_a.py::test_one", report)

    def test_only_new_failures_are_reported(self):
        self.suite(PYTEST, code=1, baseline="tests/test_a.py::test_one\ntests/test_b.py::test_two\n")
        res, report = self.check()
        self.assertEqual(res.gaps, ["new failure in repo: tests/test_c.py::test_three"])
        self.assertEqual(res.status, "blocked")
        self.assertIn("2 known failing test(s) in repo (baseline)", report)    # under "Not checked"

    def test_failures_that_are_all_known_pass(self):
        self.suite(PYTEST, code=1,
                   baseline="tests/test_a.py::test_one\ntests/test_b.py::test_two\ntests/test_c.py::test_three\n")
        res, report = self.check()
        self.assertEqual(res.gaps, [])
        self.assertEqual(res.status, "done")
        self.assertIn("every failure is in the baseline", report)

    def test_without_a_baseline_every_failure_is_new(self):
        self.suite(PYTEST, code=1)
        self.assertEqual(len(self.check()[0].gaps), 3)

    def test_a_failing_suite_that_names_no_tests_is_a_gap_with_the_tail(self):
        self.suite("Traceback ...\nImportError: no module named app\n", code=2)
        [gap] = self.check()[0].gaps
        self.assertIn("exited 2, but no failing test names were found", gap)
        self.assertIn("ImportError: no module named app", gap)

    def test_a_timeout_blocks(self):
        self.suite("", suite_timeout_min=0.0003, suite_cmd="sleep 5")
        [gap] = self.check()[0].gaps
        self.assertIn("did not finish", gap)

    def test_no_suite_cmd_blocks_with_a_reason(self):
        self.write_config(extra='[repos."../repo"]\nforbid_literals = ["x"]\n')
        [gap] = self.check()[0].gaps
        self.assertIn("no suite_cmd for repo", gap)

    def test_config_validation(self):
        self.write_config(extra='[repos."../repo"]\nsuite_cmd = ""\n')
        with self.assertRaises(ConfigError):
            load_config(self.run_dir / "baton.toml")
        self.write_config(extra='[repos."../repo"]\nsuite_cmd = "x"\nsuite_failure_regex = "no group"\n')
        with self.assertRaises(ConfigError):
            load_config(self.run_dir / "baton.toml")


class ScheduleTest(SuiteFixture):
    def plan(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        return cfg, errors, tasks

    def test_added_after_the_last_task_of_a_feature_that_touched_the_repo(self):
        self.suite("ok\n", baseline="")
        self.add_task("a", extra='feature = "f1"\n')
        self.add_task("b", deps=["a"], extra='feature = "f1"\n')
        self.add_task("other")
        cfg, errors, tasks = self.plan()
        self.assertEqual(errors, [])
        [suite] = [t for t in tasks if t.template == "repo-suite"]
        self.assertEqual((suite.id, suite.depends_on, suite.auto), ("repo-suite-f1-repo", ["a", "b"], True))
        self.assertEqual(suite.params["branch"], "feat/x")
        ids = [t.id for t in tasks]
        self.assertLess(ids.index("b"), ids.index(suite.id))
        self.assertIn("repo-suite-f1-repo  [verify: repo-suite, auto]", plan_text(cfg, tasks))

    def test_one_per_feature(self):
        self.suite("ok\n", baseline="")
        self.add_task("a", extra='feature = "f1"\n')
        self.add_task("b", extra='feature = "f2"\n')
        _, _, tasks = self.plan()
        self.assertEqual(sorted(t.id for t in tasks if t.template == "repo-suite"),
                         ["repo-suite-f1-repo", "repo-suite-f2-repo"])

    def test_not_added_without_a_suite_cmd_without_a_feature_or_when_written_by_hand(self):
        self.write_config(extra='[repos."../repo"]\nforbid_literals = ["x"]\n')
        self.add_task("a", extra='feature = "f1"\n')
        self.assertFalse([t for t in self.plan()[2] if t.template == "repo-suite"])
        self.suite("ok\n", baseline="")
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\n+++\nBuild a.\n')
        self.assertFalse([t for t in self.plan()[2] if t.template == "repo-suite"])
        self.add_task("a", extra='feature = "f1"\n')
        (self.run_dir / "verifiers").mkdir()
        (self.run_dir / "verifiers" / "S-repo.md").write_text(
            '+++\nkind = "verify"\ntemplate = "repo-suite"\nrepo = "../repo"\nfeature = "f1"\ndepends_on = ["a"]\n+++\n')
        self.assertEqual([t.id for t in self.plan()[2] if t.template == "repo-suite"], ["S-repo"])

    def test_a_checkpoint_waits_for_it_but_work_after_the_checkpoint_does_not(self):
        self.suite("ok\n", baseline="")
        self.add_task("a", extra='feature = "f1"\n')
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\nfeature = "f1"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.add_task("after", deps=["gate"], extra='feature = "f1"\n')
        _, errors, tasks = self.plan()
        self.assertEqual(errors, [])
        by_id = {t.id: t for t in tasks}
        self.assertEqual(by_id["gate"].depends_on, ["a", "repo-suite-f1-repo"])
        self.assertEqual(by_id["repo-suite-f1-repo"].depends_on, ["a"])

    def test_plan_fails_when_the_baseline_file_is_missing(self):
        self.suite("ok\n")
        (self.run_dir / "baton.toml").write_text(
            (self.run_dir / "baton.toml").read_text() + 'suite_baseline = "nope.txt"\n')
        self.add_task("a", extra='feature = "f1"\n')
        _, errors, _ = self.plan()
        self.assertTrue(any("baseline file" in e and "nope.txt" in e for e in errors), errors)


class RunTest(SuiteFixture):
    def test_it_runs_after_the_feature_gates_dependents_and_reports_in_summary(self):
        self.suite(PYTEST, code=1, baseline="tests/test_a.py::test_one\ntests/test_b.py::test_two\n")
        self.add_task("a", extra='feature = "f1"\n')
        self.add_task("b", deps=["a"], extra='feature = "f1"\n')
        self.add_task("next", deps=["repo-suite-f1-repo"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["repo-suite-f1-repo"].status, "blocked")
        self.assertEqual(self.state.tasks["next"].status, "gated")
        self.assertEqual(len(self.calls()), 2)            # the suite ran once, not inside each task
        report = (self.run_dir / "results" / "repo-suite-f1-repo.md").read_text()
        self.assertIn("new failure in repo: tests/test_c.py::test_three", report)
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("repo-suite-f1-repo (verify: repo-suite)", summary)
        self.assertIn("2 known failing test(s) in repo (baseline)", summary)     # under Not verified

    def test_all_known_failures_let_the_next_task_run(self):
        self.suite(PYTEST, code=1,
                   baseline="tests/test_a.py::test_one\ntests/test_b.py::test_two\ntests/test_c.py::test_three\n")
        self.add_task("a", extra='feature = "f1"\n')
        self.add_task("next", deps=["repo-suite-f1-repo"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "n.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["next"].status, "done")


if __name__ == "__main__":
    unittest.main()
