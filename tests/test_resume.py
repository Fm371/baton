"""Resume uses the owner, fresh sessions and real Git; no model calls."""
import contextlib
import io
import json
import signal
import time
from unittest import mock

from baton import cli, gitutil, ops
from baton.config import load_config
from baton.process import _StallWatch
from baton.runner import Runner
from baton.state import load_state, save_state
from tests.test_runner import FakeClock, git
from tests.test_signals import SignalFixture


class ResumeTest(SignalFixture):
    def test_live_resume_preserves_context_and_rejects_duplicate(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        flag = self.root / "ready"
        self.scenario_is(codex=[{"dirty": "source.py", "progress": "DONE: build; NEXT: inspect tests",
                                 "stdout": [{"type": "item.completed", "item": {
                                     "type": "command_execution", "command": "build", "exit_code": 0}}],
                                 "touch_ready": str(flag), "ignore_term": True, "hang": 60},
                                {"dirty": "finish.py", "status": "done"}])
        r, tasks = self.runner()
        r.kill_grace_s = 0.05
        seen = []

        def resume():
            for _ in range(2):
                seen.append(ops.request_resume(r.cfg, "a"))
        t = self.when_started(flag, resume)
        start = time.monotonic()
        self.assertEqual(self.finish_run(r, tasks), 0)
        t.join()
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.state.task("a").attempts, 2)
        prompt = self.calls()[1]["prompt"]
        for text in ("operator resume", "DONE: build", "build", "inspect", "Preserved HEAD"):
            self.assertIn(text, prompt)
        self.assertTrue((r.cfg.results_dir / "a" / "attempt-01.codex.out").exists())
        self.assertIn("source.py", git(self.repo, "ls-tree", "--name-only", "feat/x"))
        self.assertEqual(self.state.task("a").status, "done")
        self.assertTrue(self.state.task("a").interruptions)
        metrics = [json.loads(line) for line in (r.cfg.results_dir / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual([m["outcome"] for m in metrics], ["interrupted", "success"])
        self.assertIn("already", seen[1])

    def stopped_with_dependencies(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        flag = self.root / "ready"
        self.scenario_is(codex=[{"dirty": "source.py", "progress": "NEXT: continue saved work",
                                 "write": {str(self.repo / ".gitignore"): "node_modules/\n",
                                           str(self.repo / "node_modules" / "pkg" / "data"): "keep bytes"},
                                 "touch_ready": str(flag), "hang": 60},
                                {"dirty": "finish.py", "status": "done"}])
        r, tasks = self.runner()
        t = self.when_started(flag, lambda: r._signal(signal.SIGINT, None))
        self.assertEqual(self.finish_run(r, tasks), 3)
        t.join()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        return r.cfg

    def test_resume_after_ctrl_c_preserves_dependency_output_and_attempt_numbers(self):
        cfg = self.stopped_with_dependencies()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["resume", "a", "-c", str(cfg.base_dir / "baton.toml")])
        self.assertEqual(rc, 0)
        st = load_state(cfg.state_dir / "run.json")
        self.assertEqual(st.task("a").attempts, 2)
        self.assertEqual(st.task("a").status, "done")
        self.assertIn("NEXT: continue saved work", self.calls()[1]["prompt"])
        self.assertEqual((self.repo / "node_modules/pkg/data").read_text(), "keep bytes")
        self.assertNotIn("node_modules", git(self.repo, "ls-tree", "-r", "--name-only", "feat/x"))
        git(self.repo, "checkout", "feat/x")
        self.assertTrue(gitutil.is_ignored(self.repo, self.repo / "node_modules/pkg/data"))

    def test_refused_resume_preserves_tracked_edits_and_untracked_conflicts(self):
        cfg = self.stopped_with_dependencies()
        (self.repo / "README").write_bytes(b"unrelated edit\x00\n")
        (self.repo / "source.py").write_bytes(b"untracked conflict\x00\n")
        before = {p: (self.repo / p).read_bytes() for p in ("README", "source.py", "node_modules/pkg/data")}
        r = Runner(cfg, clock=FakeClock(), only="a", resume=True, echo=False)
        self.assertNotEqual(r.run(), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual(before, {p: (self.repo / p).read_bytes() for p in before})
        note = load_state(cfg.state_dir / "run.json").task("a").note
        self.assertIn("feat/x", note)
        self.assertIn("README", note)

    def test_resume_cannot_bypass_verifier_or_approval(self):
        self.add_task("a")
        self.add_verifier("v", deps=[])
        self.add_task("b", deps=["v"])
        cfg = load_config(self.run_dir / "baton.toml")
        st = load_state(cfg.state_dir / "run.json")
        st.task("b").status, st.task("b").attempts = "failed", 1
        st.task("v").status = "blocked"
        save_state(cfg.state_dir / "run.json", st)
        before = (cfg.state_dir / "run.json").read_bytes()
        r = Runner(cfg, only="b", resume=True, echo=False)
        self.assertEqual(r.run(), 5)
        self.assertEqual(before, (cfg.state_dir / "run.json").read_bytes())
        self.assertEqual(self.calls(), [])

    def test_status_and_watch_report_stderr_and_successful_command_activity(self):
        self.add_task("a")
        r, tasks = self.runner()
        st = r.state.task("a")
        st.status, st.attempts = "running", 1
        save_state(r.state_path, r.state)
        d = r.cfg.results_dir / "a"
        d.mkdir(parents=True)
        (d / "attempt-01.claude.out").write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "build", "exit_code": 0}}) + "\n")
        (d / "attempt-01.claude.err").write_text("waiting for provider\n")
        from baton.watch import activity_lines, watch
        lines = "\n".join(activity_lines(r.cfg, active=True, now=time.time() + 1800))
        for text in ("last output", "inactivity", "build", "waiting for provider", "baton resume a"):
            self.assertIn(text, lines)
        out = io.StringIO()
        watch(r.cfg, out, follow=False)
        self.assertIn("last output", out.getvalue())


class StallClockTest(SignalFixture):
    def test_suspend_jump_grants_fresh_observation_interval(self):
        out, err = self.root / "out", self.root / "err"
        out.write_text("build complete\n")
        err.write_text("")
        with mock.patch("baton.process.time.monotonic", return_value=0) as mono:
            w = _StallWatch(60, out, err)
            w.check()
            mono.return_value = 3600
            self.assertFalse(w.check()[0])
            mono.return_value = 3615
            self.assertFalse(w.check()[0])
            mono.return_value = 3630
            self.assertFalse(w.check()[0])
            mono.return_value = 3645
            self.assertFalse(w.check()[0])
            mono.return_value = 3660
            self.assertTrue(w.check()[0])

    def test_stderr_growth_resets_stall_and_disabled_watchdog_stays_off(self):
        out, err = self.root / "out", self.root / "err"
        out.write_text("")
        err.write_text("")
        with mock.patch("baton.process.time.monotonic", return_value=0) as mono:
            w = _StallWatch(4, out, err)
            off = _StallWatch(0, out, err)
            w.check()
            mono.return_value = 3
            err.write_text("still working\n")
            self.assertFalse(w.check()[0])
            mono.return_value = 6
            self.assertFalse(w.check()[0])
            mono.return_value = 7
            self.assertEqual(w.check(), (True, "still working"))
            mono.return_value = 999999
            self.assertFalse(off.check()[0])


class ResumeConcurrencyTest(SignalFixture):
    stopped_with_dependencies = ResumeTest.stopped_with_dependencies
    def test_multi_lane_resume_keeps_other_provider_and_shared_repo_serial(self):
        self.write_config(lanes=2)
        self.add_task("a", extra='providers = ["codex"]\n')
        self.add_task("b", extra='providers = ["claude"]\n')
        self.add_task("c", deps=["a"], extra='providers = ["codex"]\n')
        repo2 = self.root / "repo2"
        git(self.root, "clone", "-q", str(self.repo), str(repo2))
        git(repo2, "config", "user.name", "test")
        git(repo2, "config", "user.email", "test@example.com")
        p = self.run_dir / "tasks" / "b.md"
        p.write_text(p.read_text().replace('../repo"', '../repo2"').replace('feat/x', 'feat/y'))
        flag, release = self.root / "ready", self.root / "release"
        self.scenario_is(codex=[{"dirty": "a.py", "touch_ready": str(flag), "hang": 60},
                                {"dirty": "a2.py", "status": "done", "touch": str(release)},
                                {"dirty": "c.py", "status": "done"}],
                         claude=[{"wait_for": str(release), "dirty": "b.py", "status": "done"}])
        r, tasks = self.runner()
        t = self.when_started(flag, lambda: ops.request_resume(r.cfg, "a"))
        self.assertEqual(self.finish_run(r, tasks), 0)
        t.join()
        self.assertEqual([self.state.task(tid).status for tid in ("a", "b", "c")], ["done"] * 3)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(self.state.task("b").attempts, 1)
        self.assertEqual(self.state.task("c").attempts, 1)
        self.assertEqual(git(repo2, "branch", "--show-current"), "develop")

    def test_stale_resume_from_crashed_owner_cannot_interrupt_new_attempt(self):
        cfg = self.stopped_with_dependencies()
        ops.queue_op(cfg, {"op": "resume", "task": "a", "run": "crashed-owner", "attempt": 1})
        r = Runner(cfg, clock=FakeClock(), only="a", resume=True, echo=False)
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("stale resume", (cfg.logs_dir / "run.log").read_text())

    def test_second_launch_during_branch_restoration_cannot_write_or_start(self):
        self.add_task("a")
        self.scenario_is(claude=[{"dirty": "a.py", "status": "done"}])
        r, tasks = self.runner()
        old_restore = r.restore_branches
        seen = []

        def restore():
            self.assertEqual(json.loads((r.cfg.state_dir / "lock").read_text())["phase"], "cleanup")
            before = r.state_path.read_bytes()
            second = Runner(r.cfg, only="a", resume=True, echo=False)
            with mock.patch("baton.runner.time.sleep"):
                seen.append(second.run(tasks))
            self.assertEqual(before, r.state_path.read_bytes())
            old_restore()
        r.restore_branches = restore
        self.assertEqual(r.run(tasks), 0)
        self.assertEqual(seen, [4])
        self.assertEqual(len(self.calls()), 1)

    def test_live_approval_gate_refuses_without_control_request(self):
        self.add_task("a")
        (self.run_dir / "tasks" / "ok.md").write_text('+++\nkind = "approve"\n+++\nAccept?\n')
        self.add_task("b", deps=["ok"])
        r, tasks = self.runner()
        r.ordered = tasks
        r.state.task("ok").status = "awaiting"
        r.state.task("b").status, r.state.task("b").attempts = "running", 1
        save_state(r.state_path, r.state)
        from baton.watch import lock_info
        with mock.patch("baton.ops.run_active", return_value=True), \
                mock.patch("baton.watch.lock_info", return_value={"control": "resume-v1", "run": "owner"}):
            with self.assertRaisesRegex(ops.OpsError, "ok"):
                ops.request_resume(r.cfg, "b")
        self.assertFalse((r.cfg.state_dir / "ops").exists())
        self.assertIsNone(lock_info(r.cfg))
        self.assertEqual(load_state(r.state_path).task("ok").status, "awaiting")

    def test_old_runner_without_control_is_refused_without_signals(self):
        self.add_task("a")
        r, _ = self.runner()
        with mock.patch("baton.ops.run_active", return_value=True), \
                mock.patch("baton.watch.lock_info", return_value={"run": "old"}), \
                mock.patch("baton.process.os.killpg") as kill:
            with self.assertRaisesRegex(ops.OpsError, "does not advertise"):
                ops.request_resume(r.cfg, "a")
        kill.assert_not_called()
        self.assertFalse((r.cfg.state_dir / "ops").exists())


class ResumeClockAndRecoveryTest(SignalFixture):
    def test_total_timeout_and_stall_exclude_a_suspend_observation_gap(self):
        import subprocess
        self.add_task("a")
        r, tasks = self.runner()
        r.cfg.task_timeout_min, r.cfg.stall_minutes = 1, 0.05
        out, err = self.root / "out", self.root / "err"
        out.write_text("build complete\n")
        err.write_text("")
        mono = [0]
        deltas = iter([1, 3600, 1, None])
        child = mock.Mock()

        def wait(timeout):
            delta = next(deltas)
            if delta is None:
                return 0
            mono[0] += delta
            raise subprocess.TimeoutExpired("fake", timeout)
        child.wait.side_effect = wait
        r._child = child
        with mock.patch("baton.process.time.monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(r, "_kill") as kill:
            result = r._wait_for_agent(tasks[0], None, out, err)
        kill.assert_not_called()
        self.assertEqual(result.code, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.stalled)

    def test_unknown_leftover_identity_refuses_without_branch_switch_or_spawn(self):
        from baton.process import group_alive
        self.add_task("a")
        r, tasks = self.runner()
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        st = r.state.task("a")
        st.status, st.attempts, st.start_head = "running", 1, gitutil.head(self.repo)
        r.state.initial_branches[str(self.repo)] = "develop"
        p = self.leftover_process()
        r.state.agents["a"] = {"pid": p.pid, "pgid": p.pid, "repo": str(self.repo), "start": None}
        save_state(r.state_path, r.state)
        self.scenario_is(claude=[{"status": "done"}])
        r = Runner(r.cfg, only="a", resume=True, echo=False)
        self.assertEqual(r.run(tasks), 3)
        self.assertTrue(group_alive(p.pid))
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feat/x")
        self.assertEqual(self.calls(), [])
        self.assertIn("cannot establish ownership", (r.cfg.logs_dir / "run.log").read_text())

    def leftover_process(self):
        import subprocess
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_untracked_collision_alone_refuses_and_preserves_all_bytes(self):
        cfg = ResumeTest.stopped_with_dependencies(self)
        (self.repo / "source.py").write_bytes(b"untracked conflict\x00\n")
        before = (self.repo / "source.py").read_bytes()
        r = Runner(cfg, only="a", resume=True, echo=False)
        self.assertEqual(r.run(), 2)
        self.assertEqual((self.repo / "source.py").read_bytes(), before)
        self.assertEqual((self.repo / "node_modules/pkg/data").read_text(), "keep bytes")
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("source.py", load_state(r.state_path).task("a").note)

    def test_ignore_transition_never_overwrites_even_ignored_dependency_file(self):
        cfg = ResumeTest.stopped_with_dependencies(self)
        git(self.repo, "checkout", "feat/x")
        git(self.repo, "add", "-f", "node_modules/pkg/data")
        git(self.repo, "commit", "-q", "-m", "fixture: collision with saved ignored output")
        git(self.repo, "checkout", "develop")
        p = self.repo / "node_modules/pkg/data"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"new dependency bytes\x00")
        r = Runner(cfg, only="a", resume=True, echo=False)
        self.assertEqual(r.run(), 2)
        self.assertEqual(p.read_bytes(), b"new dependency bytes\x00")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual(len(self.calls()), 1)


class WatchdogBudgetTest(SignalFixture):
    def test_one_retry_budget_survives_runner_restart(self):
        self.add_task("a")
        self.write_config(stall_minutes=0.001)
        r, tasks = self.runner()
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        st = r.state.task("a")
        st.status, st.attempts, st.start_head, st.stall_retries = "running", 1, gitutil.head(self.repo), 1
        save_state(r.state_path, r.state)
        self.scenario_is(claude=[{"stdout": ["quiet again"], "hang": 60}])
        r = Runner(r.cfg, clock=FakeClock(), echo=False)
        self.assertEqual(self.finish_run(r, tasks), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.state.task("a").attempts, 2)
        self.assertEqual(self.state.task("a").status, "failed")
        self.assertIn("stalled again", self.state.task("a").note)

    def test_task_opt_out_retains_a_long_quiet_command(self):
        import subprocess
        self.add_task("a", extra="stall_minutes = 0\n")
        self.write_config(stall_minutes=0.001)
        r, tasks = self.runner()
        out, err = self.root / "out", self.root / "err"
        out.write_text("long command started\n")
        err.write_text("")
        mono = [0]
        calls = [0]
        child = mock.Mock()

        def wait(timeout):
            calls[0] += 1
            if calls[0] == 40:
                return 0
            mono[0] += 1
            raise subprocess.TimeoutExpired("fake quiet build", timeout)
        child.wait.side_effect = wait
        r._child = child
        with mock.patch("baton.process.time.monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(r, "_kill") as kill:
            result = r._wait_for_agent(tasks[0], None, out, err)
        kill.assert_not_called()
        self.assertEqual(result.code, 0)
        self.assertFalse(result.stalled)


class ResumeControlWaitTest(SignalFixture):
    def test_live_cli_waits_for_exactly_one_replacement(self):
        self.add_task("a")
        flag = self.root / "ready"
        self.scenario_is(claude=[{"dirty": "a.py", "touch_ready": str(flag), "hang": 60},
                                 {"dirty": "b.py", "status": "done"}])
        r, tasks = self.runner()
        replies = []
        t = self.when_started(flag, lambda: replies.append(cli.main([
            "resume", "a", "-c", str(r.cfg.base_dir / "baton.toml")])))
        self.assertEqual(self.finish_run(r, tasks), 0)
        t.join()
        self.assertEqual(replies, [0])
        self.assertEqual(len(self.calls()), 2)

    def test_unresponsive_owner_has_a_bounded_concrete_reason(self):
        self.add_task("a")
        r, _ = self.runner()
        st = r.state.task("a")
        st.status, st.attempts = "running", 1
        save_state(r.state_path, r.state)
        mono = iter([0, 2])
        with mock.patch("baton.ops.run_active", return_value=True), \
                mock.patch("baton.ops.time.monotonic", side_effect=lambda: next(mono)):
            with self.assertRaisesRegex(ops.OpsError, "not acknowledged.*still holds the run lock"):
                ops.wait_resume(r.cfg, "a", 1, timeout=1)
        self.assertEqual(self.calls(), [])

    def test_wait_allows_crash_recovery_only_after_lock_release(self):
        self.add_task("a")
        r, _ = self.runner()
        with mock.patch("baton.ops.run_active", return_value=False):
            self.assertFalse(ops.wait_resume(r.cfg, "a", 1))
        self.assertEqual(self.calls(), [])


class ResumeDuplicateOfflineTest(SignalFixture):
    def test_old_offline_request_cannot_replace_an_attempt_that_already_advanced(self):
        self.add_task("a")
        r, tasks = self.runner()
        st = r.state.task("a")
        st.status, st.attempts = "partial", 2
        save_state(r.state_path, r.state)
        before = r.state_path.read_bytes()
        r = Runner(r.cfg, only="a", resume=True, resume_attempt=1, echo=False)
        self.assertEqual(r.run(tasks), 0)
        self.assertEqual(before, r.state_path.read_bytes())
        self.assertEqual(self.calls(), [])


class ResumeBranchCrashTest(SignalFixture):
    def test_crash_before_failed_attempt_branch_cleanup_keeps_original_branch_record(self):
        self.add_task("a")
        r, tasks = self.runner()
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        st = r.state.task("a")
        st.status, st.attempts, st.start_head = "failed", 1, gitutil.head(self.repo)
        r.state.initial_branches[str(self.repo)] = "develop"
        save_state(r.state_path, r.state)
        self.scenario_is(claude=[{"dirty": "finished.py", "status": "done"}])
        r = Runner(r.cfg, only="a", resume=True, echo=False)
        self.assertEqual(r.run(tasks), 0)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")



class ResumeSourceCLITest(SignalFixture):
    def test_fresh_cli_process_resumes_saved_dependency_work(self):
        import subprocess
        import sys
        from pathlib import Path
        cfg = ResumeTest.stopped_with_dependencies(self)
        proc = subprocess.run([sys.executable, "-m", "baton", "resume", "a", "-c", str(cfg.base_dir / "baton.toml")],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=12)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(load_state(cfg.state_dir / "run.json").task("a").attempts, 2)
        self.assertEqual((self.repo / "node_modules/pkg/data").read_bytes(), b"keep bytes")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")

    def test_watchdog_overrides_reload_before_next_attempt(self):
        self.add_task("a", extra="stall_minutes = 20\n")
        r, tasks = self.runner()
        task = tasks[0]
        self.add_task("a", extra="stall_minutes = 0\n")
        r.reload_for_attempt(task)
        self.assertEqual(task.stall_minutes, 0)
        cfg_path = self.run_dir / "baton.toml"
        cfg_path.write_text(cfg_path.read_text().replace("[run]", "[run]\nstall_minutes = 30"))
        r.reload_for_attempt(task)
        self.assertEqual(r.cfg.stall_minutes, 30)


class ResumeResultHistoryTest(SignalFixture):
    def test_an_old_done_result_cannot_finish_a_new_attempt_that_writes_no_result(self):
        self.write_config(no_result_retries=0)
        self.add_task("a")
        r, tasks = self.runner()
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        st = r.state.task("a")
        st.status, st.attempts, st.start_head = "running", 1, gitutil.head(self.repo)
        save_state(r.state_path, r.state)
        r.cfg.results_dir.mkdir(parents=True, exist_ok=True)
        old_result = r.cfg.results_dir / "a.md"
        old_result.write_text("STATUS: done\nold attempt's report\n")
        self.scenario_is(claude=[{}])
        r = Runner(r.cfg, only="a", resume=True, clock=FakeClock(), echo=False)
        # Deliberately recent mtime exercises the reader's one-second filesystem tolerance.
        import os
        os.utime(old_result, (time.time() + 1, time.time() + 1))
        r.run(tasks)
        self.assertNotEqual(load_state(r.state_path).task("a").status, "done")
        self.assertEqual((r.cfg.results_dir / "a" / "attempt-01.a.md").read_text(),
                         "STATUS: done\nold attempt's report\n")
        self.assertEqual(len(self.calls()), 1)


class CleanupAndIdleVisibilityTest(SignalFixture):
    def test_resume_during_branch_cleanup_waits_for_release_before_offline_launch(self):
        self.add_task("a")
        r, _ = self.runner()
        st = r.state.task("a")
        st.status, st.attempts = "running", 1
        save_state(r.state_path, r.state)
        seen = []
        replacement = mock.Mock()
        replacement.run.return_value = 0

        def wait(*args):
            seen.append("lock released after branch cleanup")
            return False

        def start(*args, **kwargs):
            seen.append("replacement runner")
            return replacement
        with mock.patch("baton.watch.run_active", return_value=True), \
                mock.patch("baton.watch.lock_info", return_value={"phase": "cleanup", "control": "resume-v1"}), \
                mock.patch("baton.ops.wait_resume", side_effect=wait), \
                mock.patch("baton.ops.request_resume") as request, \
                mock.patch("baton.runner.Runner", side_effect=start), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["resume", "a", "-c", str(r.cfg.base_dir / "baton.toml")]), 0)
        self.assertEqual(seen, ["lock released after branch cleanup", "replacement runner"])
        self.assertIn("released", out.getvalue())
        request.assert_not_called()

    def test_watch_with_no_attempt_explains_the_wait_and_never_claims_a_stall(self):
        from baton.watch import watch
        self.add_task("a")
        r, _ = self.runner()
        active = [True]
        out = io.StringIO()
        rc = watch(r.cfg, out, active=lambda: active[0], sleep=lambda _: active.__setitem__(0, False))
        self.assertEqual(rc, 1)
        self.assertIn("waiting for work/approval or provider availability", out.getvalue())
        self.assertNotIn("suspected stall", out.getvalue())


class StallCrashBudgetTest(SignalFixture):
    def test_watchdog_budget_is_durable_before_child_termination(self):
        import subprocess
        from baton.process import RunStopped
        self.write_config(stall_minutes=0.001)
        self.add_task("a")
        r, tasks = self.runner()
        r.state.task("a").status, r.state.task("a").attempts = "running", 1
        out, err = self.root / "out", self.root / "err"
        out.write_text("waiting for db\n")
        err.write_text("")
        mono = [0]
        child = mock.Mock()

        def wait(timeout):
            mono[0] += 1
            raise subprocess.TimeoutExpired("fake provider", timeout)
        child.wait.side_effect = wait
        r._child = child
        with mock.patch("baton.process.time.monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(r, "_kill", side_effect=RunStopped("simulated crash at termination")):
            with self.assertRaises(RunStopped):
                r._wait_for_agent(tasks[0], None, out, err)
        self.assertEqual(load_state(r.state_path).task("a").stall_retries, 1)


class WatchClockStepTest(SignalFixture):
    def test_watch_follows_replacement_even_when_wall_clock_and_mtime_move_back(self):
        import os
        from baton.watch import latest_attempt, watch
        self.add_task("a")
        r, tasks = self.runner()
        r.state.task("a").status = "running"
        save_state(r.state_path, r.state)
        folder = r.cfg.results_dir / "a"
        folder.mkdir(parents=True)
        old = folder / "attempt-01.claude.out"
        old.write_text("old activity\n")
        os.utime(old, (10000, 10000))
        ticks = []
        newer = folder / "attempt-02.claude.out"

        def tick(_):
            ticks.append(1)
            if len(ticks) == 1:
                newer.write_text("replacement activity after clock step\n")
                os.utime(newer, (100, 100))
        out = io.StringIO()
        self.assertEqual(watch(r.cfg, out, sleep=tick, active=lambda: len(ticks) < 3), 0)
        self.assertEqual(latest_attempt(r.cfg), newer)
        self.assertIn("replacement activity after clock step", out.getvalue())

        r.state.task("a").attempts = 2
        with ops.exclusive(r.cfg):
            ops.prepare_resume(r, tasks[0])
        prompt = r.build_prompt(tasks[0], "claude", 3, r.state.task("a").last_end, r.cfg.results_dir / "a.md")
        self.assertIn("last activity: replacement activity after clock step", prompt)


class ResumeQueuedOperationTest(SignalFixture):
    def test_resume_poll_preserves_a_mark_until_the_running_task_finishes(self):
        self.add_task("a")
        self.add_task("b")
        flag = self.root / "ready"
        self.scenario_is(claude=[{"dirty": "a.py", "touch_ready": str(flag), "hang": 60},
                                 {"dirty": "a2.py", "status": "done"},
                                 {"dirty": "b.py", "status": "done"}])
        r, tasks = self.runner()

        def operate():
            ops.queue_op(r.cfg, {"op": "mark", "task": "a", "status": "blocked", "note": "operator decision"})
            ops.request_resume(r.cfg, "a")
        t = self.when_started(flag, operate)
        self.assertEqual(self.finish_run(r, tasks), 0)
        t.join()
        self.assertEqual(self.state.task("a").status, "blocked")
        self.assertIn("operator decision", self.state.task("a").note)
        self.assertEqual(self.state.task("b").status, "done")
        self.assertEqual(len(self.calls()), 3)

    def test_preflight_uses_the_interrupted_task_branch_for_shared_repo_recovery(self):
        from baton.preflight import preflight
        self.add_task("a", extra='priority = 10\n')
        self.add_task("z")
        taskfile = self.run_dir / "tasks" / "a.md"
        taskfile.write_text(taskfile.read_text().replace("feat/x", "feat/pending"))
        r, tasks = self.runner()
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "fixture: saved ignore rule")
        st = r.state.task("z")
        st.status, st.attempts, st.start_head = "running", 1, gitutil.head(self.repo)
        save_state(r.state_path, r.state)
        git(self.repo, "checkout", "develop")
        deps = self.repo / "node_modules/pkg"
        deps.mkdir(parents=True)
        (deps / "data").write_text("keep bytes")
        errors, _, _ = preflight(r.cfg)
        self.assertEqual(errors, [])
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual((deps / "data").read_text(), "keep bytes")


class FollowResumeTest(SignalFixture):
    def resume_from_idle_follow(self, lanes):
        self.write_config(lanes=lanes)
        self.add_task("a")
        r, tasks = self.runner(follow=True)
        self.scenario_is(claude=[{"dirty": "saved.py", "status": "partial", "progress": "NEXT: finish saved work"},
                                 {"dirty": "finished.py", "status": "done", "touch": str(r.stop_file)}])
        replies = []
        class OperatorClock(FakeClock):
            def sleep_until(self, when, should_stop=lambda: False):
                if not replies:
                    replies.append(ops.request_resume(r.cfg, "a"))
                else:
                    r.stop_file.write_text("stop after observing idle recovery")
                return super().sleep_until(when, should_stop)
        r.clock = OperatorClock()
        self.assertEqual(self.finish_run(r, tasks), 0)
        self.assertEqual(self.state.task("a").status, "done")
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("NEXT: finish saved work", self.calls()[1]["prompt"])
        self.assertIn("resume queued", replies[0])

    def test_serial_idle_follow_requeues_an_unfinished_task_without_a_child_to_kill(self):
        self.resume_from_idle_follow(1)

    def test_multilane_idle_follow_requeues_an_unfinished_task_without_a_child_to_kill(self):
        self.resume_from_idle_follow(2)


class ShortWakeGapTest(SignalFixture):
    def test_a_short_suspend_gap_near_the_stall_deadline_grants_a_fresh_interval(self):
        import subprocess
        self.write_config(stall_minutes=1)
        self.add_task("a")
        r, tasks = self.runner()
        out, err = self.root / "out", self.root / "err"
        out.write_text("quiet build\n")
        err.write_text("")
        mono = [0]
        intervals = iter([*([1] * 55), 10, None])
        child = mock.Mock()

        def wait(timeout):
            interval = next(intervals)
            if interval is None:
                return 0
            mono[0] += interval
            raise subprocess.TimeoutExpired("provider", timeout)
        child.wait.side_effect = wait
        r._child = child
        with mock.patch("baton.process.time.monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(r, "_kill") as kill:
            result = r._wait_for_agent(tasks[0], None, out, err)
        kill.assert_not_called()
        self.assertFalse(result.stalled)
        self.assertEqual(result.code, 0)
