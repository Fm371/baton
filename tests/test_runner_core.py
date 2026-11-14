"""Characterization tests for the core of the runner: how a task is finished (the decision table of
`Runner.finish`) and what `Runner.run` cleans up, and in which order. They pin behavior that the
end-to-end tests only touch in passing, so the functions behind them can be taken apart safely."""

import fcntl
import signal
import unittest
from types import SimpleNamespace
from unittest import mock

from baton import gitutil
from baton.config import load_config
from baton.finishing import FinishFacts, decide_finish
from baton.preflight import preflight
from baton.runner import Runner, _codex_why, _keys_message
from baton.state import load_state
from tests.test_runner import FakeClock, RunnerFixture


class FinishTableTest(RunnerFixture):
    """`Runner.finish` with the git layer stubbed: only the decision and what is recorded are examined."""

    def build(self, verifier=False):
        self.add_task("a")
        if verifier:
            self.add_verifier("v", deps=("a",))
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        r = Runner(cfg, clock=FakeClock(), echo=False)
        return r, {t.id: t for t in tasks}

    def finish(self, status="done", note="", commits=2, left=None, refused=(), suspect=(), stray=None,
               verifier=False, started=True):
        r, by_id = self.build(verifier)
        task = by_id["v" if verifier else "a"]
        ts = r.state.task(task.id)
        ts.start_head = "abc"
        ts.suspect_files = list(suspect)
        r.refused[task.id] = list(refused)
        with mock.patch.object(gitutil, "is_repo", return_value=True), \
                mock.patch.object(gitutil, "count_commits", return_value=commits), \
                mock.patch.object(gitutil, "changed_paths", return_value=stray or []), \
                mock.patch.object(gitutil, "is_dirty", return_value=False), \
                mock.patch.object(r, "commit_leftovers", return_value=left) as leftovers:
            r.finish(task, status, note, started=started)
        return ts, leftovers

    def test_done_with_commits_is_done_and_quiet(self):
        ts, _ = self.finish("done", "")
        self.assertEqual((ts.status, ts.note, ts.commits), ("done", "", 2))
        self.assertTrue(ts.finished)

    def test_done_without_commits_is_done_with_a_note(self):
        ts, _ = self.finish("done", "", commits=0)
        self.assertEqual((ts.status, ts.note), ("done", "no commits"))

    def test_partial_without_commits_fails(self):
        ts, _ = self.finish("partial", "ran out of time", commits=0)
        self.assertEqual((ts.status, ts.note), ("failed", "ran out of time; partial with no commits"))

    def test_partial_with_commits_stays_partial(self):
        ts, _ = self.finish("partial", "half", commits=1)
        self.assertEqual((ts.status, ts.note), ("partial", "half"))

    def test_blocked_and_failed_keep_their_status(self):
        for status in ("blocked", "failed"):
            ts, _ = self.finish(status, "why", commits=0)
            self.assertEqual((ts.status, ts.note), (status, "why"))

    def test_work_that_could_not_be_committed_is_not_done(self):
        ts, _ = self.finish("done", "", left="hook refused")
        self.assertEqual((ts.status, ts.note), ("partial", "not all work could be committed: hook refused"))

    def test_refused_files_are_listed_five_at_most(self):
        files = [f"k{i}.pem" for i in range(7)]
        ts, _ = self.finish("done", "x", refused=files)
        self.assertEqual(ts.note, "x; 7 file(s) not committed because they look like secrets: "
                                  "k0.pem, k1.pem, k2.pem, k3.pem, k4.pem …")

    def test_committed_suspect_files_are_flagged(self):
        ts, _ = self.finish("done", "", suspect=["secrets.py"])
        self.assertEqual(ts.note, "committed secret-looking files: secrets.py (check before pushing)")

    def test_a_verifier_that_touched_non_test_files_is_blocked(self):
        ts, _ = self.finish("done", "", verifier=True, stray=["app.py", "lib.py"])
        self.assertEqual((ts.status, ts.note), ("blocked", "verifier touched non-test files: app.py, lib.py"))

    def test_a_task_that_never_started_commits_nothing_and_counts_nothing(self):
        ts, leftovers = self.finish("failed", "cannot prepare repository", commits=9, started=False)
        leftovers.assert_not_called()
        self.assertEqual((ts.status, ts.commits, ts.note), ("failed", 0, "cannot prepare repository"))

    def test_finish_is_written_to_state_and_the_log(self):
        self.finish("done", "")
        state = load_state(self.run_dir / "state" / "run.json")
        self.assertEqual(state.tasks["a"].status, "done")
        self.assertIn("task a: done (2 commits", (self.run_dir / "logs" / "run.log").read_text())


class DecideFinishTest(unittest.TestCase):
    """The same rules as a pure function, including the order they apply in."""

    def test_rules_apply_in_order(self):
        facts = FinishFacts(commits=0, left="boom", refused=["a.pem"], suspect=["secrets.py"])
        status, note = decide_finish("done", "", facts)
        # uncommitted work turns done into partial, and a partial with no commits then fails
        self.assertEqual(status, "failed")
        self.assertEqual(note, "not all work could be committed: boom; "
                               "1 file(s) not committed because they look like secrets: a.pem; "
                               "committed secret-looking files: secrets.py (check before pushing); "
                               "partial with no commits")

    def test_a_verifier_blocks_after_everything_else(self):
        facts = FinishFacts(commits=1, is_verifier=True, stray=["x.py"])
        self.assertEqual(decide_finish("done", "n", facts), ("blocked", "n; verifier touched non-test files: x.py"))

    def test_a_non_agent_task_without_commits_gets_no_note(self):
        self.assertEqual(decide_finish("done", "", FinishFacts(commits=0, runs_agent=False)), ("done", ""))


class RunCleanupTest(RunnerFixture):
    def runner(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        return Runner(cfg, clock=FakeClock(), echo=False), tasks

    def test_cleanup_runs_in_order_and_releases_everything(self):
        r, tasks = self.runner()
        seen = []
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        real_wait = r.notifier.wait
        real_restore = r.restore_branches

        def wait():
            seen.append(("notifier.wait", r.run_is_live))
            real_wait()

        def restore():
            seen.append(("restore_branches", r.run_is_live))
            real_restore()

        r.notifier.wait, r.restore_branches = wait, restore
        self.assertEqual(r.run(tasks), 0)
        # the run is no longer live when the cleanup starts; notifications are flushed before branches go back
        self.assertEqual(seen, [("notifier.wait", False), ("restore_branches", False)])
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)       # handlers put back
        self.assertFalse(r.stop_file.exists())
        with (r.cfg.state_dir / "lock").open("a+") as lock:                      # the lock was released
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_cleanup_also_runs_when_the_run_crashes(self):
        r, tasks = self.runner()
        seen = []
        r.restore_branches = lambda: seen.append("restore_branches")
        with mock.patch.object(r, "_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                r.run(tasks)
        self.assertEqual(seen, ["restore_branches"])
        with (r.cfg.state_dir / "lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class SharedLoopStepsTest(RunnerFixture):
    """The steps the serial loop and the lanes dispatcher share."""

    def runner(self):
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        return Runner(cfg, clock=FakeClock(), echo=False), tasks

    def test_waiting_for_new_work_is_announced_once(self):
        r, _ = self.runner()
        go_on, idle = r.wait_for_new_work(False)
        self.assertEqual((go_on, idle), (True, True))
        go_on, idle = r.wait_for_new_work(idle)
        self.assertEqual((go_on, idle), (True, True))
        log = (r.cfg.logs_dir / "run.log").read_text()
        self.assertEqual(log.count("nothing to run; waiting for new tasks and approvals"), 1)

    def test_a_stop_during_the_wait_ends_it(self):
        r, _ = self.runner()
        with mock.patch.object(r.clock, "sleep_until", return_value=False):
            self.assertEqual(r.wait_for_new_work(False), (False, True))

    def test_decided_checkpoints_leave_the_pending_list_in_place(self):
        r, tasks = self.runner()
        pending = list(tasks)
        same = pending
        with mock.patch.object(r, "apply_decisions", return_value={tasks[0].id}):
            r.drop_decided(pending)
        self.assertEqual(pending, [])
        self.assertIs(pending, same)


class HelperTextTest(unittest.TestCase):
    def test_what_a_codex_attempt_left_behind(self):
        att = SimpleNamespace(timed_out=False, stalled=False, exit_code=3)
        limit, auth, other = (SimpleNamespace(kind=k) for k in ("limit", "auth", "success"))
        self.assertEqual(_codex_why(att, other, "done"), "left uncommitted at the end of the task")
        self.assertEqual(_codex_why(att, limit, None), "codex hit its usage limit")
        self.assertEqual(_codex_why(att, auth, None), "codex authentication error")
        self.assertEqual(_codex_why(SimpleNamespace(timed_out=True, stalled=False, exit_code=1), other, None),
                         "timed out")
        self.assertEqual(_codex_why(SimpleNamespace(timed_out=False, stalled=True, exit_code=1), other, None),
                         "stalled (no output)")
        self.assertEqual(_codex_why(att, other, None), "ended without a result file (exit 3)")

    def test_the_hardcoded_keys_message(self):
        class Found(SimpleNamespace):
            def __str__(self):
                return f"{self.name}:{self.line}"
        plain = Found(name="a.py", line=3)
        banned = Found(name="FORBIDDEN-PATTERN", line=9)
        with mock.patch("baton.runner.keycheck.FORBIDDEN", "FORBIDDEN-PATTERN"):
            only_plain = _keys_message([plain])
            only_banned = _keys_message([banned])
            both = _keys_message([plain, banned])
        self.assertTrue(only_plain.startswith(
            "baton found string literals that look like hardcoded data keys:\n  a.py:3\n"))
        self.assertTrue(only_banned.startswith(
            "baton found literals this repository forbids (`forbid_literals` in baton.toml):\n  "))
        self.assertTrue(both.startswith(
            "baton found string literals that look like hardcoded data keys and literals this repository "
            "forbids (`forbid_literals` in baton.toml):\n"))
        for text in (only_plain, only_banned, both):
            self.assertTrue(text.endswith("Then write the result file again."))


if __name__ == "__main__":
    unittest.main()
