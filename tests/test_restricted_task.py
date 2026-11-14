"""A task whose providers list excludes the current provider does not move the run."""

from tests.test_runner import RunnerFixture


class RestrictedTaskTest(RunnerFixture):
    def test_a_claude_only_task_leaves_current_on_codex(self):
        self.write_config(start_provider="codex")
        self.add_task("F03", extra='providers = ["claude"]\n')
        self.add_task("next", deps=["F03"])
        self.scenario_is(claude=[{"commit": "f.txt", "status": "done"}],
                         codex=[{"commit": "n.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["F03"].providers, ["claude"])
        self.assertEqual(self.state.tasks["next"].providers, ["codex"])
        self.assertIsNone(self.state.current)         # nothing is mid-attempt at the end: no stale value is kept
        self.assertIn("F03 runs on claude (providers list; current stays codex)", self.log)
        self.assertNotIn("switch codex -> claude", self.log)
        self.assertNotIn("limited", self.log)
