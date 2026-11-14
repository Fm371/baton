"""required_env: checked by plan and before a task starts; skipped-for-env results are blocked:env."""

import os
import unittest
from unittest import mock

from baton.config import ConfigError, env_names, load_config
from baton.preflight import preflight
from baton.requiredenv import missing_for, skipped_for_env
from baton.runner import Runner
from baton.state import load_state
from tests.test_runner import FakeClock, RunnerFixture

VAR = "BATON_TEST_MONGO_URI"


class SkippedForEnvTest(unittest.TestCase):
    def test_a_skip_line_that_names_an_unset_variable_is_found(self):
        text = "tests/test_db.py::test_x SKIPPED (BATON_TEST_MONGO_URI not set)\n12 passed, 9 skipped"
        self.assertEqual(skipped_for_env(text), "BATON_TEST_MONGO_URI is not set")

    def test_the_generic_wording_counts_too(self):
        self.assertIsNotNone(skipped_for_env("All Mongo tests skipped: the environment variable is missing"))

    def test_a_variable_named_in_prose_without_a_skip_does_not(self):
        self.assertIsNone(skipped_for_env("I set BATON_TEST_MONGO_URI and ran everything: 40 passed."))
        self.assertIsNone(skipped_for_env("3 skipped (slow tests), SOME_CONFIG is documented"))

    def test_env_names_are_validated(self):
        self.assertEqual(env_names(["A_B", "A_B", "c1"], "x"), ["A_B", "c1"])
        with self.assertRaises(ConfigError):
            env_names(["not a name"], "x")


class RequiredEnvTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(VAR, None)

    def cfg(self):
        return load_config(self.run_dir / "baton.toml")

    def run_without_preflight_errors(self):
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        self.clock = FakeClock()
        rc = Runner(cfg, clock=self.clock, echo=False).run(tasks)
        self.state = load_state(cfg.state_dir / "run.json")
        return rc

    def test_global_task_and_provider_lists_are_all_checked(self):
        self.write_config(required_env=[VAR])
        self.add_task("a", extra='required_env = ["OTHER_NEEDED_VAR"]\n')
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        self.assertEqual(missing_for(cfg, tasks[0]), [VAR, "OTHER_NEEDED_VAR"])
        os.environ[VAR] = "mongodb://x"
        self.assertEqual(missing_for(cfg, tasks[0]), ["OTHER_NEEDED_VAR"])

    def test_a_provider_env_table_satisfies_the_variable_for_that_provider(self):
        self.write_config(required_env=[VAR])
        toml = (self.run_dir / "baton.toml").read_text()
        toml = toml.replace("[providers.claude]", f'[providers.claude]\nenv = {{ {VAR} = "mongodb://c" }}', 1)
        (self.run_dir / "baton.toml").write_text(toml)
        self.add_task("a", extra='providers = ["claude"]\n')
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        self.assertEqual(missing_for(cfg, tasks[0]), [])
        self.assertEqual(missing_for(cfg, tasks[0], "claude"), [])

    def test_plan_reports_a_missing_variable_as_an_error(self):
        self.write_config(required_env=[VAR])
        self.add_task("a")
        errors, _, _ = preflight(self.cfg())
        self.assertTrue(any(VAR in e and "task a" in e and "skipped" in e for e in errors), errors)
        os.environ[VAR] = "x"
        errors, _, _ = preflight(self.cfg())
        self.assertEqual(errors, [])

    def test_a_task_that_already_finished_is_not_reported(self):
        self.write_config(required_env=[VAR])
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        os.environ[VAR] = "x"
        self.assertEqual(self.run_baton(), 0)
        del os.environ[VAR]
        errors, _, _ = preflight(self.cfg())
        self.assertEqual(errors, [])

    def test_the_run_stops_before_an_attempt_when_the_variable_is_gone(self):
        self.write_config(required_env=[VAR])
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_without_preflight_errors(), 3)        # stopped, not failed
        self.assertEqual(self.calls(), [])                                # no attempt, no quota spent
        self.assertNotIn("a", [t for t, s in self.state.tasks.items() if s.attempts])
        self.assertIn(VAR, (self.run_dir / "logs" / "run.log").read_text())

    def test_a_verifier_that_reports_env_skips_is_blocked_by_the_environment(self):
        os.environ[VAR] = "x"                                             # the shell has it; the tests still skipped
        self.write_config()
        self.add_task("a")
        self.add_verifier("va", deps=("a",))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done",
                                  "result_extra": "tests/test_db.py::test_x SKIPPED (BATON_TEST_MONGO_URI not set)"}])
        self.run_baton()
        va = self.state.tasks["va"]
        self.assertEqual(va.status, "blocked")
        self.assertEqual(va.reason, "env")
        self.assertIn("blocked:env", va.note)

    def test_a_clean_verifier_is_done_and_has_no_reason(self):
        self.write_config()
        self.add_task("a")
        self.add_verifier("va", deps=("a",))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["va"].status, "done")
        self.assertEqual(self.state.tasks["va"].reason, "")

    def test_an_implementer_that_finished_done_is_not_touched_by_a_skip_line(self):
        self.write_config()
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done",
                                  "result_extra": "2 DB tests skipped: BATON_TEST_MONGO_URI not set"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["a"].status, "done")


if __name__ == "__main__":
    unittest.main()
