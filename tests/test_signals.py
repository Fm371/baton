"""Signals and processes: a closed terminal, an agent that ignores SIGTERM, what an agent leaves running,
an agent left by a run that died, the repo-suite process group, and a wait that Ctrl+C cuts short."""

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from baton.checks.reposuite import SuiteStopped, run_suite
from baton.clock import Clock
from baton.config import load_config
from baton.preflight import preflight
from baton.process import end_group, group_alive, proc_start_time
from baton.runner import Runner
from baton.state import load_state
from tests.test_runner import FakeClock, RunnerFixture, git


def alive(pid: int) -> bool:
    """A process that is running (a zombie nobody has reaped yet does not count)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2:].split()[0] != "Z"


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class SignalFixture(RunnerFixture):
    def runner(self, **kw):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        r = Runner(cfg, clock=FakeClock(), echo=False, **kw)
        return r, tasks

    def finish_run(self, r, tasks):
        rc = r.run(tasks)
        self.state = load_state(r.cfg.state_dir / "run.json")
        self.log = (r.cfg.logs_dir / "run.log").read_text()
        return rc

    def when_started(self, flag: Path, action):
        """Run `action` from another thread once the fake agent has started (it creates `flag`)."""
        def go():
            end = time.time() + 20
            while not flag.exists() and time.time() < end:
                time.sleep(0.02)
            action()
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t


class StopSignalsTest(SignalFixture):
    def test_sighup_stops_like_ctrl_c_and_keeps_the_work(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"dirty": "wip.txt", "touch": str(flag), "hang": 60}])
        r, tasks = self.runner()
        t = self.when_started(flag, lambda: os.kill(os.getpid(), signal.SIGHUP))
        t0 = time.time()
        rc = self.finish_run(r, tasks)
        t.join()
        self.assertEqual(rc, 3)
        self.assertLess(time.time() - t0, 20)
        self.assertIn("stopped by signal 1", self.log)
        self.assertIn("wip", git(self.repo, "log", "-1", "--format=%s", "feat/x"))      # the work was committed on the task branch
        self.assertEqual(self.state.agents, {})

    def test_an_agent_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"touch_start": str(flag), "ignore_term": True, "sleep": 120}])
        r, tasks = self.runner()
        r.kill_grace_s = 0.5
        t = self.when_started(flag, lambda: r._signal(signal.SIGTERM, None))
        t0 = time.time()
        rc = self.finish_run(r, tasks)
        t.join()
        self.assertEqual(rc, 3)
        self.assertLess(time.time() - t0, 30)       # not the 120 s the agent wanted, nor the task timeout
        self.assertIn("ignored SIGTERM; sending SIGKILL", self.log)

    def test_a_second_signal_kills_at_once(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"touch_start": str(flag), "ignore_term": True, "sleep": 120}])
        r, tasks = self.runner()
        r.kill_grace_s = 300

        def twice():
            r._signal(signal.SIGINT, None)
            time.sleep(0.3)
            r._signal(signal.SIGINT, None)
        t = self.when_started(flag, twice)
        t0 = time.time()
        rc = self.finish_run(r, tasks)
        t.join()
        self.assertEqual(rc, 3)
        self.assertLess(time.time() - t0, 30)


class LeftoverProcessesTest(SignalFixture):
    def test_what_an_agent_leaves_running_is_ended_when_it_exits(self):
        self.add_task("a")
        pidfile = self.root / "bg.pid"
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "spawn": str(pidfile)}])
        r, tasks = self.runner()
        r.group_grace_s = 1.0
        self.assertEqual(self.finish_run(r, tasks), 0)
        pid = int(pidfile.read_text())
        self.assertTrue(wait_dead(pid), "the agent's background process is still running")
        self.assertIn("the agent left processes running; they were ended", self.log)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.agents, {})

    def test_the_agent_pid_is_in_run_json_while_it_runs(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch_start": str(flag), "sleep": 1}])
        r, tasks = self.runner()
        seen = {}

        def look():
            time.sleep(0.3)
            seen.update(json.loads((r.cfg.state_dir / "run.json").read_text()).get("agents", {}))
        t = self.when_started(flag, look)
        self.finish_run(r, tasks)
        t.join()
        self.assertEqual(list(seen), ["a"])
        self.assertEqual(seen["a"]["pgid"], seen["a"]["pid"])

    def leftover(self):
        """A process in its own session, as an agent of a run that died would be."""
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def write_agents(self, p, start):
        (self.run_dir / "state").mkdir(exist_ok=True)
        (self.run_dir / "state" / "run.json").write_text(json.dumps(
            {"version": 2, "tasks": {}, "agents": {"old": {"pid": p.pid, "pgid": p.pid, "start": start,
                                                           "repo": str(self.repo)}}}))

    def test_an_agent_left_by_a_run_that_died_is_ended_before_the_next_run_starts(self):
        self.add_task("a")
        old = self.leftover()
        self.write_agents(old, proc_start_time(old.pid))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        r, tasks = self.runner()
        r.group_grace_s = 1.0
        reaper = threading.Thread(target=old.wait)      # its parent would reap it
        reaper.start()
        self.assertEqual(self.finish_run(r, tasks), 0)
        reaper.join(timeout=5)
        self.assertFalse(reaper.is_alive(), "the leftover agent is still running")
        self.assertIn("left running by a run that died", self.log)
        self.assertEqual(self.state.agents, {})

    def test_a_recycled_pid_is_left_alone(self):
        self.add_task("a")
        other = self.leftover()
        self.write_agents(other, "1")               # the recorded start time is not this process's
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        r, tasks = self.runner()
        self.assertEqual(self.finish_run(r, tasks), 0)
        self.assertIsNone(other.poll())             # still running
        self.assertEqual(self.state.agents, {})

    def test_end_group_and_group_alive(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertTrue(group_alive(p.pid))
        t = threading.Thread(target=p.wait)         # reap it, as the agent's parent does
        t.start()
        self.assertTrue(end_group(p.pid, 2.0))
        t.join()
        self.assertFalse(group_alive(p.pid))
        self.assertFalse(end_group(p.pid, 2.0))


class RepoSuiteGroupTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def child_script(self):
        pidfile = self.dir / "child.pid"
        return f"sleep 300 & echo $! > {pidfile}; wait", pidfile

    def test_a_timeout_ends_the_runner_below_the_shell(self):
        cmd, pidfile = self.child_script()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_suite(cmd, self.dir, 0.5, lambda: False)
        self.assertTrue(wait_dead(int(pidfile.read_text())), "the test runner below the shell survived")

    def test_a_stop_does_not_wait_for_the_timeout(self):
        cmd, pidfile = self.child_script()
        t0 = time.time()
        with self.assertRaises(SuiteStopped):
            run_suite(cmd, self.dir, 3600, lambda: pidfile.exists())
        self.assertLess(time.time() - t0, 10)
        self.assertTrue(wait_dead(int(pidfile.read_text())))

    def test_output_and_exit_code_are_returned(self):
        code, out, err = run_suite("echo hi; echo oops >&2; exit 3", self.dir, 30, lambda: False)
        self.assertEqual((code, out.strip(), err.strip()), (3, "hi", "oops"))


class SleepTest(unittest.TestCase):
    def test_a_stop_during_a_long_wait_is_seen_within_a_second_or_two(self):
        clock = Clock()
        t0 = time.time()
        when = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertFalse(clock.sleep_until(when, lambda: time.time() - t0 > 0.3))
        self.assertLess(time.time() - t0, 3)


class InternalErrorTest(SignalFixture):
    def test_a_bug_in_one_task_is_logged_and_the_others_go_on(self):
        self.add_task("a")
        self.add_task("b")
        self.scenario_is(claude=[{"commit": "b.txt", "status": "done"}])
        r, tasks = self.runner()
        real = r._run_attempt

        def boom(task, loop, pick):
            if task.id == "a":
                raise RuntimeError("boom")
            return real(task, loop, pick)
        with mock.patch.object(r, "_run_attempt", boom):
            rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertIn("internal error: RuntimeError: boom", self.state.tasks["a"].note)
        self.assertIn("Traceback", self.log)
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_a_closed_terminal_does_not_stop_the_log(self):
        r, _ = self.runner()
        r.echo = True
        with mock.patch("builtins.print", side_effect=OSError(5, "Input/output error")):
            r.log("hello")
        self.assertFalse(r.echo)
        self.assertIn("hello", (r.cfg.logs_dir / "run.log").read_text())


class LanesBookkeepingTest(SignalFixture):
    def test_the_retries_a_task_used_survive_a_trip_back_to_the_dispatcher(self):
        self.add_task("a")
        r, tasks = self.runner()
        task = tasks[0]
        loop = r._start_task(task)
        loop.no_result_left, loop.transient_left = 0, 1
        r._requeue(task, "claude limited", loop)
        again = r._start_task(task)
        self.assertEqual((again.no_result_left, again.transient_left), (0, 1))
        self.assertEqual(r._budgets, {})

    def test_an_assigned_provider_does_not_ask_the_scheduler(self):
        self.add_task("a")
        r, tasks = self.runner()
        task = tasks[0]
        loop = r._start_task(task)
        loop.fixed = "claude"
        with mock.patch.object(r.sched, "pick", side_effect=AssertionError("pick() changes the scheduler")):
            step, pick = r._choose_provider(task, loop, r.clock.now())
        self.assertEqual(pick.provider, "claude")


if __name__ == "__main__":
    unittest.main()
