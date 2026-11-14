"""`baton stop`: finish the current task, then end cleanly."""

from baton import cli
from tests.test_runner import FakeClock, RunnerFixture, claude_limit, codex_limit


class StopClock(FakeClock):
    """Writes the stop file the first time the run sleeps, as `baton stop` would."""

    def __init__(self, stop_file):
        super().__init__()
        self.stop_file = stop_file

    def sleep_until(self, when, should_stop=lambda: False):
        self.stop_file.write_text("x")
        if should_stop():
            return False
        return super().sleep_until(when, should_stop)


class StopTest(RunnerFixture):
    def stop_file(self):
        return self.run_dir / "state" / "stop"

    def baton(self, *args):
        return cli.main([args[0], "-c", str(self.run_dir / "baton.toml"), *args[1:]])

    def test_the_task_in_progress_finishes_and_the_next_one_does_not_start(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        (self.run_dir / "state").mkdir()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch": str(self.stop_file())},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["b"].status, "pending")
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("run ended cleanly: stop requested with `baton stop`; 1 task(s) left for the next run", self.log)
        self.assertFalse(self.stop_file().exists())            # consumed
        self.assertEqual(self.run_baton(), 0)                  # the next run continues with b
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_a_wait_for_a_reset_is_cut_short_and_the_task_stays_resumable(self):
        self.add_task("a")
        (self.run_dir / "state").mkdir()
        self.scenario_is(claude=[claude_limit(reset="10:40pm (Asia/Tehran)")],
                         codex=[codex_limit("11:30 PM")])
        self.assertEqual(self.run_baton(clock=StopClock(self.stop_file())), 0)
        self.assertIn("run ended cleanly", self.log)
        self.assertIn(self.state.tasks["a"].status, ("running", "waiting"))
        self.assertNotEqual(self.state.tasks["a"].status, "blocked")

    def test_a_stale_stop_file_does_not_stop_the_next_run(self):
        self.add_task("a")
        (self.run_dir / "state").mkdir()
        self.stop_file().write_text("x")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")

    def test_the_command_says_so_when_nothing_runs(self):
        self.add_task("a")
        self.assertEqual(self.baton("stop"), 1)
        self.assertFalse(self.stop_file().exists())

    def test_the_command_writes_the_stop_file_while_a_run_holds_the_lock(self):
        import fcntl
        (self.run_dir / "state").mkdir()
        with (self.run_dir / "state" / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.baton("stop"), 0)
        self.assertTrue(self.stop_file().is_file())
