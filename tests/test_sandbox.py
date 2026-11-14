"""A broken Codex sandbox takes Codex out of the run; it does not block tasks."""

import os
from unittest import mock

from baton.config import load_config
from baton.preflight import preflight
from baton.sandbox import sandbox_error
from tests.test_runner import RunnerFixture


class SandboxErrorTest(RunnerFixture):
    def test_finds_the_bwrap_line(self):
        out = 'x\n{"aggregated_output": "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"}\n'
        self.assertIn("bwrap: loopback: Failed RTM_NEWADDR", sandbox_error(out))
        self.assertIsNone(sandbox_error("all tests passed, bubblewrap is fine"))

    def plan(self):
        return preflight(load_config(self.run_dir / "baton.toml"))

    def test_plan_warns_with_both_hints_and_does_not_fail(self):
        self.add_task("a")
        with mock.patch.dict(os.environ, {"BATON_FAKE_SANDBOX": "broken"}):
            errors, warnings, _ = self.plan()
        self.assertEqual(errors, [])
        text = "\n".join(w for w in warnings if "sandbox" in w)
        self.assertIn("Failed RTM_NEWADDR", text)
        self.assertIn("sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0", text)
        self.assertIn("lost on reboot", text)
        self.assertIn("inside another agent's sandbox", text)
        self.assertIn("left out of this run", text)

    def test_a_working_sandbox_passes_silently(self):
        self.add_task("a")
        _, warnings, _ = self.plan()
        self.assertFalse([w for w in warnings if "sandbox" in w])

    def test_an_unrecognised_probe_is_only_a_warning_and_codex_stays(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}])
        with mock.patch.dict(os.environ, {"BATON_FAKE_SANDBOX": "unknown"}):
            _, warnings, _ = self.plan()
            self.assertTrue(any("sandbox not checked" in w for w in warnings))
            self.run_baton()
        self.assertEqual(self.state.tasks["a"].providers, ["codex"])

    def test_the_probe_can_be_turned_off_and_skipped_without_a_sandbox(self):
        self.add_task("a")
        text = (self.run_dir / "baton.toml").read_text().replace(
            "[providers.codex]\n", "[providers.codex]\nsandbox_probe = []\n")
        (self.run_dir / "baton.toml").write_text(text)
        with mock.patch.dict(os.environ, {"BATON_FAKE_SANDBOX": "broken"}):
            _, warnings, _ = self.plan()
        self.assertFalse([w for w in warnings if "cannot start" in w])

    def test_a_broken_sandbox_at_start_leaves_codex_out_and_claude_runs_the_task(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        with mock.patch.dict(os.environ, {"BATON_FAKE_SANDBOX": "broken"}):
            self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["a"].providers, ["claude"])
        self.assertEqual([c["provider"] for c in self.calls()], ["claude"])
        self.assertIn("codex left out of this run", self.log)
        self.assertIn("kernel.apparmor_restrict_unprivileged_userns=0", self.log)

    def test_the_bwrap_error_during_an_attempt_retries_on_the_next_provider(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(
            codex=[{"bwrap": True, "status": "blocked", "result_extra": "cannot run commands"}],
            claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")      # not blocked
        self.assertEqual(self.state.tasks["a"].providers, ["codex", "claude"])
        self.assertEqual(self.state.tasks["b"].providers, ["claude"])
        self.assertEqual([c["provider"] for c in self.calls()], ["codex", "claude", "claude"])
        self.assertIn("codex left out of this run", self.log)
        self.assertEqual(self.metrics[0]["outcome"], "sandbox")
        self.assertIn("could not start its sandbox", self.calls()[1]["prompt"])
        # the stale "blocked" verdict of the failed attempt is not read as the claude attempt's
        self.assertTrue((self.run_dir / "results" / "a.md").read_text().startswith("STATUS: done"))

    def test_a_task_that_only_codex_may_run_is_blocked_with_the_sandbox_reason(self):
        self.write_config(start_provider="codex")
        self.add_task("a", extra='providers = ["codex"]\n')
        self.scenario_is(codex=[{"bwrap": True, "status": "blocked"}])
        self.run_baton()
        ts = self.state.tasks["a"]
        self.assertEqual(ts.status, "blocked")
        self.assertIn("sandbox unusable", ts.note)

    def test_a_done_task_is_kept_even_if_a_command_hit_the_error(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"bwrap": True, "dirty": "a.txt", "status": "done",
                                 "commit_message": "feat(a): add a"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["a"].providers, ["codex"])
