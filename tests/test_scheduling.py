"""A limit of one model family, the per-task emergency brake, an internal error in one lane,
and an agent that exits cleanly without a result."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from baton.config import load_config
from baton.limits import classify
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.scheduler import Candidate, ProviderStatus, Scheduler, select
from baton.state import load_state
from tests.test_lanes import LanesFixture
from tests.test_runner import FakeClock, RunnerFixture, claude_limit

T0 = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
HEAVY = """
[routing.rungs.r5]
claude = { model = "claude-opus-4", effort = "high" }
codex = { model = "sol", effort = "medium" }
"""


class ModelLimitSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.s = Scheduler(["claude", "codex"], "claude")
        self.models = {"opus": {"claude": "claude-opus-4", "codex": "sol"},
                       "sonnet": {"claude": "sonnet", "codex": "sol"}}

    def test_an_opus_limit_does_not_hold_back_a_sonnet_task(self):
        self.s.mark_model_limited("claude", "opus", T0 + 3 * H)
        p = self.s.pick(T0, models=self.models["sonnet"])
        self.assertEqual((p.provider, p.wait_until, p.switched_from), ("claude", None, None))
        self.assertTrue(self.s.available("claude", T0, "sonnet"))
        self.assertFalse(self.s.available("claude", T0, "claude-OPUS-4"))

    def test_an_opus_task_moves_to_the_other_provider(self):
        self.s.mark_model_limited("claude", "opus", T0 + 3 * H)
        p = self.s.pick(T0, models=self.models["opus"])
        self.assertEqual((p.provider, p.switched_from), ("codex", "claude"))

    def test_when_every_way_is_closed_the_wait_is_the_model_reset(self):
        self.s.mark_model_limited("claude", "opus", T0 + 3 * H)
        self.s.mark_limited("codex", T0 + 5 * H, "five_hour")
        p = self.s.pick(T0, ["claude"], models=self.models["opus"])
        self.assertEqual((p.provider, p.wait_until), ("claude", T0 + 3 * H))

    def test_without_the_model_the_old_rule_holds(self):
        self.s.mark_limited("claude", T0 + 2 * H, "five_hour")
        self.assertEqual(self.s.pick(T0, models=self.models["sonnet"]).provider, "codex")

    def test_an_expired_model_limit_is_cleared_when_the_provider_is_picked(self):
        self.s.mark_model_limited("claude", "opus", T0 + H)
        p = self.s.pick(T0 + 2 * H, models=self.models["opus"])
        self.assertEqual(p.provider, "claude")
        self.assertEqual(self.s.status["claude"].model_limits, {})

    def test_model_limits_survive_a_round_trip_through_run_json(self):
        self.s.mark_model_limited("claude", "opus", T0 + H)
        back = ProviderStatus.from_dict(self.s.status["claude"].to_dict())
        self.assertEqual(back.model_limits, {"opus": T0 + H})
        old = ProviderStatus.from_dict({"limited_until": None, "window": "", "disabled": None})
        self.assertEqual(old.model_limits, {})            # a run.json from before

    def test_lane_selection_looks_at_the_model(self):
        self.s.mark_model_limited("claude", "opus", T0 + H)
        cands = [Candidate("a", ("claude",), "la", "ra", None, (("claude", "claude-opus-4"),)),
                 Candidate("b", ("claude",), "lb", "rb", None, (("claude", "sonnet"),))]
        out = select(cands, ["claude", "codex"], self.s.status, {"claude": 2, "codex": 1}, set(), set(), {}, T0, 2)
        self.assertEqual([(c.id, p) for c, p in out], [("b", "claude")])

    def test_the_outcome_names_the_family(self):
        ev = ('{"type":"result","is_error":true,"result":"You\'ve hit your Opus limit · resets 10:40pm '
              '(Asia/Tehran)"}')
        oc = classify("claude", ev, "", 1, T0, timezone.utc, None, T0)
        self.assertEqual((oc.kind, oc.window, oc.model), ("limit", "model", "opus"))


class ModelLimitRunTest(RunnerFixture):
    def test_an_opus_limit_leaves_the_sonnet_tasks_on_claude(self):
        self.write_config(extra=HEAVY)
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nrung = "r5"\nbranch = "feat/a"\n+++\nBuild a.\n')
        (self.run_dir / "tasks" / "b.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/b"\n+++\nBuild b.\n')
        self.scenario_is(claude=[claude_limit(window="Opus"), {"commit": "b.txt", "status": "done"}],
                         codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        self.assertEqual(self.state.tasks["b"].providers, ["claude"])     # not pushed to Codex
        self.assertEqual(self.state.tasks["b"].status, "done")
        claude = self.state.providers["claude"]
        self.assertIsNone(claude["limited_until"])
        self.assertIn("opus", claude["model_limits"])
        cfg = load_config(self.run_dir / "baton.toml")
        # the limit was recorded on the fake clock (October); `now` puts the plan inside it
        self.assertIn("claude: opus limited until",
                      plan_text(cfg, preflight(cfg)[2], now=datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)))

    def test_a_session_limit_still_closes_the_whole_provider(self):
        self.write_config(extra=HEAVY)
        (self.run_dir / "tasks" / "b.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/b"\n+++\nBuild b.\n')
        self.scenario_is(claude=[claude_limit()], codex=[{"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].providers, ["claude", "codex"])
        self.assertEqual(self.state.providers["claude"].get("model_limits"), {})


class BrakeTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.write_config(quick_exit_seconds=120, max_quick_unknown=3)

    def test_one_failing_task_fails_alone_and_the_run_goes_on(self):
        self.add_task("a")
        self.add_task("b")
        self.scenario_is(claude=[{"exit": 1, "stderr": "mystery"}] * 3 + [{"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertIn("3 quick exits in a row", self.state.tasks["a"].note)
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertEqual(self.state.quick_fails, 0)                # b's result started the count over
        self.assertEqual(self.state.quick_tasks, [])
        waits = [s for s in self.clock.slept if isinstance(s, datetime)]
        self.assertEqual(len(waits), 2)                           # two waits of the three attempts, not three

    def test_different_tasks_failing_the_same_way_pull_the_brake(self):
        for tid in "abc":
            self.add_task(tid)
        self.scenario_is(claude=[{"exit": 1, "stderr": "mystery"}] * 9)
        self.assertEqual(self.run_baton(), 3)
        self.assertEqual(len(self.calls()), 3 + 3 + 1)            # a, b used their three; c stops the run
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertEqual(self.state.tasks["b"].status, "failed")
        self.assertEqual(self.state.tasks["c"].status, "stopped")
        self.assertIn("3 different tasks in a row", self.log)

    def test_a_program_that_cannot_start_stops_the_run_at_once(self):
        self.add_task("a")
        self.add_task("b")
        self.scenario_is(claude=[{"exit": 127, "stderr": "not found"}] * 3)
        self.assertEqual(self.run_baton(), 3)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("could not be started", self.log)
        self.assertEqual(self.state.tasks["b"].status, "pending")

    def test_a_clean_exit_without_a_result_does_not_wait_for_an_unknown_reason(self):
        self.add_task("a")
        self.scenario_is(claude=[{"exit": 0, "stdout": ["I cannot do this"]}] * 4)
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertIn("ended without a result file", self.state.tasks["a"].note)
        self.assertNotIn("unknown reason", self.log)
        self.assertEqual(self.state.quick_fails, 0)


class LaneInternalErrorTest(LanesFixture):
    def test_a_bug_in_one_lane_fails_that_task_only(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["claude"])
        self.scenario_is(claude=[{"commit": "b.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        r = Runner(cfg, clock=FakeClock(), echo=False)
        real = r._run_attempt

        def boom(task, loop, pick):
            if task.id == "a":
                raise RuntimeError("boom")
            return real(task, loop, pick)
        with mock.patch.object(r, "_run_attempt", boom):
            rc = r.run(tasks)
        st = load_state(cfg.state_dir / "run.json")
        log = (cfg.logs_dir / "run.log").read_text()
        self.assertEqual(rc, 0)
        self.assertEqual(st.tasks["a"].status, "failed")
        self.assertIn("internal error: RuntimeError: boom", st.tasks["a"].note)
        self.assertIn("Traceback", log)
        self.assertEqual(st.tasks["b"].status, "done")
        self.assertEqual(st.running, {})


if __name__ == "__main__":
    unittest.main()
