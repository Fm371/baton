"""A task goes back to start_provider mid-attempt once it resets; verifiers_last runs verifiers at the end."""

import json
from datetime import timedelta

from tests.test_runner import RunnerFixture, T0


class ReturnMidTaskTest(RunnerFixture):
    def seed_codex_limited(self, until, known=True):
        state = self.run_dir / "state"
        state.mkdir(exist_ok=True)
        (state / "run.json").write_text(json.dumps({
            "current": "claude", "quick_fails": 0, "tasks": {},
            "providers": {"codex": {"limited_until": until.isoformat(), "window": "5h",
                                    "disabled": None, "reset_known": known}}}))

    def test_claude_attempt_is_stopped_and_the_task_finishes_on_codex(self):
        self.write_config(start_provider="codex", return_check_seconds=0.2, return_mid_task=True)
        self.add_task("a")
        # codex resets a few fake-clock seconds after the run starts
        self.seed_codex_limited(T0 + timedelta(seconds=20))
        self.scenario_is(claude=[{"sleep": 30, "commit": "never.txt", "status": "done"}],
                         codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        provs = [c["provider"] for c in self.calls()]
        self.assertEqual(provs[0], "claude")
        self.assertEqual(provs[-1], "codex")
        self.assertIn("moves back mid-task", self.log)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertNotIn("never.txt", git_files(self.repo))

    def test_no_return_when_the_reset_time_was_only_a_guess(self):
        self.write_config(start_provider="codex", return_check_seconds=0.2, return_mid_task=True)
        self.add_task("a")
        self.seed_codex_limited(T0 + timedelta(seconds=20), known=False)
        self.scenario_is(claude=[{"sleep": 2, "commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual([c["provider"] for c in self.calls()], ["claude"])
        self.assertNotIn("moves back mid-task", self.log)

    def test_return_mid_task_is_off_by_default(self):
        from baton.config import load_config
        self.write_config(start_provider="codex", return_check_seconds=0.2)
        self.assertFalse(load_config(self.run_dir / "baton.toml").return_mid_task)
        self.add_task("a")
        self.seed_codex_limited(T0 + timedelta(seconds=20))
        self.scenario_is(claude=[{"sleep": 2, "commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual([c["provider"] for c in self.calls()], ["claude"])


class VerifiersLastTest(RunnerFixture):
    def test_a_verifier_nobody_depends_on_runs_last_and_gates_nothing(self):
        self.write_config(verifiers_last=True)
        self.add_task("a")
        self.add_verifier("va", deps=("a",))
        self.add_task("b", extra="priority = -5\n")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "blocked"}])
        self.run_baton()
        order = [c["prompt"] for c in self.calls()]
        self.assertIn("Build a.", order[0])
        self.assertIn("Build b.", order[1])          # b runs before the trailing verifier
        self.assertIn("SECRET CANARY", order[2])     # the verifier runs last
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertEqual(self.state.tasks["va"].status, "blocked")

    def test_an_explicit_depends_on_a_verifier_stays_a_real_gate(self):
        # T09 depends on V5: with verifiers_last it must still wait for V5, not run before it
        self.write_config(verifiers_last=True)
        self.add_task("t05")
        self.add_verifier("v5", deps=("t05",))
        self.add_task("t09", deps=("v5",), extra="priority = 100\n")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.run_baton()
        order = [c["prompt"] for c in self.calls()]
        self.assertIn("Build t05.", order[0])
        self.assertIn("SECRET CANARY", order[1])     # the verifier ran before t09
        self.assertIn("Build t09.", order[2])
        self.assertEqual(self.state.tasks["t09"].status, "done")

    def test_a_blocked_gate_verifier_still_gates_its_dependant(self):
        self.write_config(verifiers_last=True)
        self.add_task("t05")
        self.add_verifier("v5", deps=("t05",))
        self.add_task("t09", deps=("v5",))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "blocked"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["v5"].status, "blocked")
        self.assertEqual(self.state.tasks["t09"].status, "gated")
        self.assertEqual([c for c in self.calls() if "Build t09." in c["prompt"]], [])

    def test_only_checks_the_gate_of_a_verifier_behind_the_task(self):
        self.write_config(verifiers_last=True)
        self.add_task("t05")
        self.add_verifier("v5", deps=("t05",))
        self.add_task("t09", deps=("v5",))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "blocked"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.run_baton(only="t09"), 5)          # v5 is a real gate for t09


def git_files(repo):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "log", "--all", "--name-only", "--format="],
                          capture_output=True, text=True).stdout


class ReturnMidTaskPiecesTest(RunnerFixture):
    def test_reset_known_is_saved_and_old_state_loads_as_known(self):
        from baton.scheduler import ProviderStatus
        s = ProviderStatus(limited_until=T0, window="5h", reset_known=False)
        self.assertFalse(ProviderStatus.from_dict(s.to_dict()).reset_known)
        self.assertTrue(ProviderStatus.from_dict({"limited_until": None, "window": ""}).reset_known)

    def test_the_new_keys_are_checked(self):
        from baton.config import ConfigError, load_config
        for key, bad in (("return_mid_task", '"no"'), ("verifiers_last", '"yes"'),
                         ("return_check_seconds", "0")):
            self.write_config(**{key: json.loads(bad)})
            with self.assertRaises(ConfigError, msg=key):
                load_config(self.run_dir / "baton.toml")

    def test_no_watch_with_lanes(self):
        self.write_config(start_provider="codex", lanes=2, return_mid_task=True)
        self.add_task("a")
        from baton.config import load_config
        from baton.preflight import preflight
        from baton.runner import Runner
        from tests.test_runner import FakeClock
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        r = Runner(cfg, clock=FakeClock(), echo=False)
        task = tasks[0]
        r.sched.mark_limited("codex", T0 + timedelta(hours=1), "5h", reset_known=True)

        class Loop:
            fixed = "claude"
        self.assertIsNone(r.return_watch(task, "claude", Loop()))
        self.assertEqual(r.return_watch(task, "claude", None), "codex")
