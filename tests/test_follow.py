"""baton run --follow: keep running after the queue empties and pick up new tasks and approvals."""


from baton import cli
from baton.config import load_config
from baton.preflight import preflight
from baton.runner import Runner
from baton.state import load_state
from tests.test_runner import FakeClock, RunnerFixture


class ScriptedClock(FakeClock):
    """Runs one action each time the run goes idle, like a person working beside it."""

    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)
        self.idle_sleeps = 0

    def sleep_until(self, when, should_stop=lambda: False):
        self.idle_sleeps += 1
        if self.actions:
            self.actions.pop(0)()
        if should_stop():
            return False
        return super().sleep_until(when, should_stop)


class FollowTest(RunnerFixture):
    def baton(self, *args):
        return cli.main([args[0], "-c", str(self.run_dir / "baton.toml"), *args[1:]])

    def follow(self, clock):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        rc = Runner(cfg, clock=clock, echo=False, follow=True).run(tasks)
        self.state = load_state(cfg.state_dir / "run.json")
        self.log = (cfg.logs_dir / "run.log").read_text()
        return rc

    def stop(self):
        (self.run_dir / "state" / "stop").write_text("x")

    def test_a_new_task_file_is_picked_up_and_stop_ends_the_run(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        clock = ScriptedClock([lambda: self.add_task("b", deps=["a"]), self.stop])
        self.assertEqual(self.follow(clock), 0)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "b": "done"})
        self.assertIn("nothing to run; waiting for new tasks and approvals", self.log)
        self.assertIn("new task(s): b", self.log)
        self.assertIn("run ended cleanly", self.log)
        self.assertEqual(clock.idle_sleeps, 2)

    def test_an_approval_given_while_idle_releases_the_held_tasks(self):
        self.add_task("a")
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.add_task("c", deps=["gate"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"}])
        clock = ScriptedClock([lambda: self.assertEqual(self.baton("approve", "gate"), 0), self.stop])
        self.assertEqual(self.follow(clock), 0)
        self.assertEqual(self.state.tasks["gate"].status, "done")
        self.assertEqual(self.state.tasks["c"].status, "done")
        self.assertEqual(len(self.calls()), 2)

    def test_without_follow_the_same_run_ends_at_the_checkpoint(self):
        self.add_task("a")
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.add_task("c", deps=["gate"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["c"].status, "pending")

    def test_a_broken_new_task_file_is_logged_once_and_the_run_goes_on(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        bad = self.run_dir / "tasks" / "bad.md"
        clock = ScriptedClock([lambda: bad.write_text("no front matter"), lambda: None, self.stop])
        self.assertEqual(self.follow(clock), 0)
        self.assertEqual(self.log.count("task folders not reloaded"), 1)

    def test_a_task_that_finished_earlier_is_not_queued_again(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.run_baton()
        self.add_task("b", deps=["a"])
        self.assertEqual(self.follow(ScriptedClock([self.stop])), 0)
        self.assertEqual(len(self.calls()), 2)          # a once, b once: a was not run again
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_follow_cannot_be_combined_with_only(self):
        self.add_task("a")
        self.assertEqual(self.baton("run", "--follow", "--only", "a"), 2)
