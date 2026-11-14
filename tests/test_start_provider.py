"""start_provider wins over the saved current provider, and a new task goes
back to it once it is available again."""

import json
from datetime import timedelta

from baton.config import load_config
from baton.preflight import plan_text, preflight
from tests.test_runner import FakeClock, RunnerFixture, codex_limit, git


class JumpClock(FakeClock):
    """Jumps two hours ahead the first time `calls` agent calls have been recorded."""

    def __init__(self, fixture, calls):
        super().__init__()
        self.fixture, self.after, self.jumped = fixture, calls, False

    def now(self):
        if not self.jumped and len(self.fixture.calls()) >= self.after:
            self.jumped = True
            self.t += timedelta(hours=2)
        return super().now()


class StartProviderTest(RunnerFixture):
    def write_state(self, current, tasks=None):
        state = self.run_dir / "state"
        state.mkdir(exist_ok=True)
        (state / "run.json").write_text(json.dumps({"current": current, "providers": {}, "quick_fails": 0,
                                                    "tasks": tasks or {}}))

    def head(self):
        return git(self.repo, "rev-parse", "HEAD")

    def test_a_new_run_begins_from_start_provider_not_the_saved_current(self):
        self.write_config(start_provider="codex")
        self.write_state("claude")
        self.add_task("a")
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["codex"])
        # nothing is mid-attempt, so the stale value is simply ignored: no warning in the log
        self.assertNotIn("state says current", self.log)
        self.assertIsNone(self.state.current)               # and a clean end leaves no stale value behind

    def test_a_task_mid_attempt_continues_on_the_saved_provider(self):
        self.write_config(start_provider="codex")
        self.write_state("claude", {"a": {"status": "running", "attempts": 1, "providers": ["claude"],
                                          "start_head": self.head()}})
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.calls()[0]["provider"], "claude")

    def test_run_provider_overrides_start_provider_even_mid_attempt(self):
        self.write_config(start_provider="claude")
        self.write_state("claude", {"a": {"status": "running", "attempts": 1, "providers": ["claude"],
                                          "start_head": self.head()}})
        self.add_task("a")
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(provider="codex"), 0)
        self.assertEqual(self.calls()[0]["provider"], "codex")

    def test_run_provider_rejects_a_disabled_provider(self):
        self.add_task("a")
        text = (self.run_dir / "baton.toml").read_text().replace(
            "[providers.codex]\n", "[providers.codex]\nenabled = false\n")
        (self.run_dir / "baton.toml").write_text(text)
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"), "codex")
        self.assertTrue(any("--provider codex" in e for e in errors))

    def test_plan_says_where_the_run_begins(self):
        self.write_config(start_provider="codex")
        self.write_state("claude")
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg)
        # nothing is mid-attempt: the stale saved value is ignored, and plan does not warn about it
        self.assertFalse(any("current = claude" in w for w in warnings), warnings)
        self.assertIn("begins on: codex", plan_text(cfg, tasks))

    def test_plan_warns_when_a_mid_attempt_task_keeps_the_saved_provider(self):
        self.write_config(start_provider="codex")
        self.write_state("claude", {"a": {"status": "running", "attempts": 1, "providers": ["claude"],
                                          "start_head": self.head()}})
        self.add_task("a")
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("mid-attempt" in w and "--provider codex" in w for w in warnings), warnings)

    def test_a_new_task_returns_to_the_preferred_provider_after_its_reset(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(codex=[codex_limit("10:30 PM"), {"commit": "b.txt", "status": "done"}],
                         claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(clock=JumpClock(self, 2)), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["codex", "claude"])
        self.assertEqual(self.state.tasks["b"].providers, ["codex"])
        self.assertIn("switch claude -> codex (back to the preferred provider codex)", self.log)

    def test_a_task_mid_attempt_does_not_return_to_the_preferred_provider(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[codex_limit("10:30 PM")],
                         claude=[{"commit": "a.txt"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(clock=JumpClock(self, 2)), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["codex", "claude"])
        self.assertEqual([c["provider"] for c in self.calls()], ["codex", "claude", "claude"])

    def test_return_to_start_provider_can_be_turned_off(self):
        self.write_config(start_provider="codex", return_to_start_provider=False)
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(codex=[codex_limit("10:30 PM")],
                         claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(clock=JumpClock(self, 2)), 0)
        self.assertEqual(self.state.tasks["b"].providers, ["claude"])
