"""[run] lanes > 1: tasks in different repositories at once. Fake CLIs and a fake clock; the agents
write timestamps and wait for each other through files, so overlap is proved, not assumed."""

import json
import signal
import subprocess
import threading
import time
import unittest
from pathlib import Path

from baton import cli
from baton.config import ConfigError, load_config
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.state import RunState, TaskState, load_state, save_state
from baton.tasks import TaskError, load_tasks
from tests.test_runner import FakeClock, RunnerFixture, claude_limit, git


class LanesFixture(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.repo2 = self.make_repo("repo2")
        self.span = self.root / "span.log"
        self.write_config(lanes=2)

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "develop")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        (repo / "README").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        return repo

    def lane_task(self, tid, repo="repo", deps=(), providers=None, lane=None, branch="feat/x"):
        meta = [f'repo = "../{repo}"', 'work_type = "feature"', f'branch = "{branch}"']
        if deps:
            meta.append(f"depends_on = {json.dumps(list(deps))}")
        if providers:
            meta.append(f"providers = {json.dumps(providers)}")
        if lane:
            meta.append(f'lane = "{lane}"')
        (self.run_dir / "tasks" / f"{tid}.md").write_text("+++\n" + "\n".join(meta) + f"\n+++\nBuild {tid}.\n")

    def intervals(self):
        """[(start, end, repo name, provider)] of every agent call, from the span file."""
        out, open_ = [], {}
        for line in self.span.read_text().splitlines():
            kind, t, cwd, prov = line.split()
            if kind == "start":
                open_.setdefault((cwd, prov), []).append(float(t))
            else:
                out.append((open_[(cwd, prov)].pop(0), float(t), cwd, prov))
        return sorted(out)

    def assertSerial(self, *spans):
        spans = sorted(spans)
        for first, second in zip(spans, spans[1:], strict=False):
            self.assertLessEqual(first[1], second[0], f"{first} overlaps {second}")

    def step(self, name, **kw):
        return {"commit": f"{name}.txt", "status": "done", "span": str(self.span), **kw}


class LanesTest(LanesFixture):
    def test_two_repositories_run_at_the_same_time(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        # each agent waits for the other to start: serial execution would hit the 30 s timeout
        self.scenario_is(
            claude=[self.step("a", touch_start=str(self.root / "a.up"), wait_for=str(self.root / "b.up"))],
            codex=[self.step("b", touch_start=str(self.root / "b.up"), wait_for=str(self.root / "a.up"))])
        t0 = time.time()
        self.assertEqual(self.run_baton(), 0)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "b": "done"})
        (a_start, a_end, *_), (b_start, b_end, *_) = self.intervals()
        self.assertLess(max(a_start, b_start), min(a_end, b_end))
        self.assertIn("lanes: up to 2 tasks at once", self.log)
        self.assertIn("[repo] task a: attempt 1 on claude", self.log)
        self.assertIn("[repo2] task b: attempt 1 on codex", self.log)
        self.assertEqual({m["task"]: m["lane"] for m in self.metrics}, {"a": "repo", "b": "repo2"})
        self.assertEqual(self.state.running, {})
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("## Lanes", summary)

    def test_a_task_in_the_same_repository_waits(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo", providers=["codex"])
        self.scenario_is(claude=[self.step("a", sleep=0.4)], codex=[self.step("b", sleep=0.4)])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.intervals()), 2)
        self.assertSerial(*self.intervals())
        self.assertEqual(git(self.repo, "rev-list", "--count", "develop..feat/x"), "2")

    def test_an_explicit_lane_serializes_two_repositories(self):
        self.lane_task("a", "repo", providers=["claude"], lane="db")
        self.lane_task("b", "repo2", providers=["codex"], lane="db")
        self.scenario_is(claude=[self.step("a", sleep=0.4)], codex=[self.step("b", sleep=0.4)])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.intervals()), 2)
        self.assertSerial(*self.intervals())
        self.assertIn("[db]", self.log)

    def test_a_limit_seen_by_one_lane_stops_new_work_on_that_provider(self):
        self.lane_task("a", "repo")
        self.lane_task("b", "repo2")
        self.scenario_is(claude=[claude_limit(reset="10:40pm (Asia/Tehran)")],
                         codex=[self.step("a"), self.step("b")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual([c["provider"] for c in self.calls()], ["claude", "codex", "codex"])
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        self.assertEqual(self.state.tasks["b"].providers, ["codex"])
        self.assertIn("switch claude -> codex", self.log)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "b": "done"})
        self.assertIsNotNone(self.state.providers["claude"]["limited_until"])

    def test_a_task_after_a_dependency_in_another_repository_waits_for_it(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", deps=["a"], providers=["codex"])
        self.scenario_is(claude=[self.step("a", sleep=0.4)], codex=[self.step("b")])
        self.assertEqual(self.run_baton(), 0)
        self.assertSerial(*self.intervals())
        self.assertEqual([c["provider"] for c in self.calls()], ["claude", "codex"])

    def test_a_blocked_dependency_skips_the_dependent_in_the_other_lane(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", deps=["a"], providers=["codex"])
        self.scenario_is(claude=[{"status": "blocked"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual((self.state.tasks["a"].status, self.state.tasks["b"].status), ("blocked", "skipped"))
        self.assertEqual([c["provider"] for c in self.calls()], ["claude"])

    def test_a_verifier_that_is_not_done_gates_the_task_in_the_other_lane(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("c", "repo2", deps=["v"], providers=["codex"])
        d = self.run_dir / "verifiers"
        d.mkdir()
        (d / "v.md").write_text('+++\nkind = "verify"\nrepo = "../repo"\nwork_type = "feature"\n'
                                'depends_on = ["a"]\n+++\nCheck rounding.\n')
        self.scenario_is(claude=[self.step("a"), {"status": "blocked"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual((st["a"].status, st["v"].status), ("done", "blocked"))
        self.assertEqual((st["c"].status, st["c"].gated_by), ("gated", "v"))
        self.assertEqual([c["provider"] for c in self.calls()], ["claude", "claude"])

    def test_a_verifier_that_passes_releases_the_task_in_the_other_lane(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("c", "repo2", deps=["v"], providers=["codex"])
        d = self.run_dir / "verifiers"
        d.mkdir()
        (d / "v.md").write_text('+++\nkind = "verify"\nrepo = "../repo"\nwork_type = "feature"\n'
                                'depends_on = ["a"]\n+++\nCheck rounding.\n')
        self.scenario_is(claude=[self.step("a"), {"status": "done", "commit": "tests/test_v.py"}],
                         codex=[self.step("c")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "v": "done", "c": "done"})

    def test_crash_recovery_with_two_running_tasks(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["claude"])
        st = RunState()
        for name, repo in (("a", self.repo), ("b", self.repo2)):
            git(repo, "checkout", "-q", "-b", "feat/x", "develop")
            (repo / "leftover.txt").write_text("half done\n")
            st.tasks[name] = TaskState(status="running", attempts=1, start_head=git(repo, "rev-parse", "HEAD"),
                                       providers=["claude"], started="2026-10-24T18:00:00+00:00")
        st.running = {"a": {"lane": "repo", "provider": "claude"}}          # stale: the run died
        (self.run_dir / "state").mkdir(exist_ok=True)
        save_state(self.run_dir / "state" / "run.json", st)
        self.scenario_is(claude=[self.step("a"), self.step("b")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.log.count("recovered uncommitted work"), 2)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "b": "done"})
        for repo, tid in ((self.repo, "a"), (self.repo2, "b")):
            self.assertIn(f"chore({tid}): wip", git(repo, "log", "--format=%s", "feat/x"))
        self.assertEqual(self.state.running, {})

    def test_the_watch_repos_warning_skips_a_repository_another_lane_is_working_in(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        self.write_config(lanes=2, watch_repos=["../repo2"])
        # repo2 becomes dirty while its task still runs: a's end must not warn about it
        self.scenario_is(
            claude=[self.step("a", wait_for=str(self.root / "b.up"))],
            codex=[self.step("b", touch_start=str(self.root / "b.up"), sleep=1.5)])

        def dirty():
            end = time.time() + 20
            while not (self.root / "b.up").exists() and time.time() < end:
                time.sleep(0.02)
            (self.repo2 / "stray.txt").write_text("dirty on purpose\n")

        threading.Thread(target=dirty, daemon=True).start()
        self.assertEqual(self.run_baton(), 0)
        self.assertNotIn("uncommitted changes in repo2", self.state.tasks["a"].note)


class LanesWaitTest(LanesFixture):
    def test_with_nothing_running_it_sleeps_until_the_reset_of_the_only_allowed_provider(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.scenario_is(claude=[claude_limit(reset="10:40pm (Asia/Tehran)"), self.step("a")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("sleeping until", self.log)
        self.assertIn("resuming after reset on claude", self.log)
        self.assertEqual([c["provider"] for c in self.calls()], ["claude", "claude"])
        self.assertTrue(any(not isinstance(x, (int, float)) for x in self.clock.slept))      # slept until a time

    def test_a_task_that_may_not_use_the_other_provider_is_blocked_when_the_wait_is_too_long(self):
        self.write_config(lanes=2, max_sleep_hours=0.1)
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        self.scenario_is(claude=[claude_limit(reset="10:40pm (Asia/Tehran)")], codex=[self.step("b")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual((self.state.tasks["a"].status, self.state.tasks["b"].status), ("blocked", "done"))
        self.assertIn("more than 0.1h away", self.state.tasks["a"].note)

    def test_every_provider_limited_for_longer_than_max_sleep_hours_ends_the_run(self):
        self.write_config(lanes=2, max_sleep_hours=0.1)
        self.lane_task("a", "repo")
        self.scenario_is(claude=[claude_limit(reset="10:40pm (Asia/Tehran)")],
                         codex=[{"exit": 1, "stdout": [{"type": "turn.failed", "error": {"message":
                                 "You've hit your usage limit. Upgrade to Pro or try again at 11:30 PM."}}]}])
        self.assertEqual(self.run_baton(), 3)
        self.assertIn("more than 0.1h away", self.log)
        self.assertEqual(self.state.tasks["a"].status, "waiting")


class LanesFollowTest(LanesFixture):
    def test_follow_picks_up_a_new_task_file_and_an_approval(self):
        from tests.test_follow import ScriptedClock
        self.lane_task("a", "repo", providers=["claude"])
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.lane_task("c", "repo2", deps=["gate"], providers=["claude"])
        stop = self.run_dir / "state" / "stop"
        (self.run_dir / "state").mkdir(exist_ok=True)
        self.scenario_is(claude=[self.step("a"), self.step("c"), self.step("d")])
        clock = ScriptedClock([lambda: self.lane_task("d", "repo2", providers=["claude"]),
                               lambda: cli.main(["approve", "gate", "-c", str(self.run_dir / "baton.toml")]),
                               lambda: stop.write_text("x")])
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(Runner(cfg, clock=clock, echo=False, follow=True).run(tasks), 0)
        st = load_state(cfg.state_dir / "run.json")
        self.assertEqual({t: s.status for t, s in st.tasks.items()},
                         {"a": "done", "gate": "done", "c": "done", "d": "done"})


class LanesStopTest(LanesFixture):
    def runner(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        return Runner(cfg, clock=FakeClock(), echo=False), tasks

    def run_and(self, runner, tasks, act):
        """Run in this thread; `act` runs on another thread once both agents have started."""
        worker = threading.Thread(target=act, daemon=True)
        worker.start()
        rc = runner.run(tasks)
        worker.join(5)
        return rc

    def wait_for_files(self, *paths, timeout=20):
        end = time.time() + timeout
        while time.time() < end and not all(Path(p).exists() for p in paths):
            time.sleep(0.02)
        return all(Path(p).exists() for p in paths)

    def test_a_signal_stops_both_agents_and_each_lane_saves_its_work(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        up = [str(self.root / "a.up"), str(self.root / "b.up")]
        self.scenario_is(claude=[{"sleep": 60, "touch_start": up[0]}], codex=[{"sleep": 60, "touch_start": up[1]}])
        r, tasks = self.runner()

        def act():
            self.assertTrue(self.wait_for_files(*up))
            (self.repo / "a.txt").write_text("half done\n")        # what each agent had written so far
            (self.repo2 / "b.txt").write_text("half done\n")
            r._signal(signal.SIGTERM, None)

        t0 = time.time()
        self.assertEqual(self.run_and(r, tasks, act), 3)
        self.assertLess(time.time() - t0, 30)           # both sleeping agents were killed
        st = load_state(self.run_dir / "state" / "run.json")
        for tid, repo in (("a", self.repo), ("b", self.repo2)):
            self.assertEqual(st.tasks[tid].status, "running")
            self.assertIn("stopped", st.tasks[tid].last_end)
            self.assertTrue(git(repo, "log", "-1", "--format=%s", "feat/x").startswith(f"chore({tid}): wip"))
        self.assertEqual(st.running, {})

    def test_the_emergency_brake_in_one_lane_stops_the_other(self):
        self.write_config(lanes=2, quick_exit_seconds=9999, max_quick_unknown=1)
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        self.scenario_is(claude=[{"exit": 1, "stdout": ["boom"]}],
                         codex=[{"sleep": 60, "touch_start": str(self.root / "b.up")}])
        t0 = time.time()
        self.assertEqual(self.run_baton(), 3)
        self.assertLess(time.time() - t0, 30)
        self.assertIn("emergency brake", self.log)
        self.assertEqual(self.state.tasks["a"].status, "stopped")
        self.assertEqual(self.state.tasks["b"].status, "running")       # resumable, not failed

    def test_baton_stop_lets_every_busy_lane_finish_and_starts_nothing_new(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        self.lane_task("c", "repo", deps=["a"], providers=["claude"])
        (self.run_dir / "state").mkdir(exist_ok=True)
        stop = self.run_dir / "state" / "stop"
        self.scenario_is(claude=[self.step("a", sleep=0.5, touch_start=str(self.root / "a.up")), self.step("c")],
                         codex=[self.step("b", sleep=0.5, touch_start=str(self.root / "b.up"))])
        r, tasks = self.runner()

        def act():
            self.assertTrue(self.wait_for_files(self.root / "a.up", self.root / "b.up"))
            stop.write_text("x")

        self.assertEqual(self.run_and(r, tasks, act), 0)
        st = load_state(self.run_dir / "state" / "run.json")
        self.assertEqual((st.tasks["a"].status, st.tasks["b"].status), ("done", "done"))
        self.assertEqual(st.tasks["c"].status, "pending")
        self.assertIn("run ended cleanly", (self.run_dir / "logs" / "run.log").read_text())


class LanesConfigTest(LanesFixture):
    def test_lanes_default_to_one(self):
        self.write_config()
        cfg = load_config(self.run_dir / "baton.toml")
        self.assertEqual((cfg.lanes, cfg.providers["claude"].max_concurrent), (1, 1))

    def test_lanes_and_slots_are_read(self):
        path = self.run_dir / "baton.toml"
        path.write_text(path.read_text().replace("[providers.claude]\n", "[providers.claude]\nmax_concurrent = 2\n"))
        cfg = load_config(path)
        self.assertEqual((cfg.lanes, cfg.providers["claude"].max_concurrent), (2, 2))

    def test_bad_values_are_refused(self):
        for bad in (0, -1, "2", 1.5, True):
            self.write_config(lanes=bad)
            with self.assertRaisesRegex(ConfigError, "run.lanes"):
                load_config(self.run_dir / "baton.toml")
        self.write_config(lanes=2)
        path = self.run_dir / "baton.toml"
        path.write_text(path.read_text().replace("[providers.codex]\n", "[providers.codex]\nmax_concurrent = 0\n"))
        with self.assertRaisesRegex(ConfigError, "providers.codex.max_concurrent"):
            load_config(path)

    def test_the_lane_key_of_a_task(self):
        self.lane_task("a", "repo")
        self.lane_task("b", "repo2", lane="shared-db")
        self.lane_task("c", "repo/", lane=None)
        tasks = {t.id: t for t in load_tasks(load_config(self.run_dir / "baton.toml"))}
        self.assertEqual(tasks["a"].lane_key, str(self.repo.resolve()))
        self.assertEqual(tasks["c"].lane_key, tasks["a"].lane_key)          # same repository, one lane
        self.assertEqual((tasks["b"].lane_key, tasks["b"].lane_name), ("lane:shared-db", "shared-db"))
        self.assertEqual(tasks["a"].lane_name, "repo")

    def test_a_subfolder_of_a_repository_is_the_same_lane(self):
        (self.repo / "sub").mkdir()
        self.lane_task("a", "repo")
        self.lane_task("c", "repo/sub")
        tasks = {t.id: t for t in load_tasks(load_config(self.run_dir / "baton.toml"))}
        self.assertEqual(tasks["a"].lane_key, tasks["c"].lane_key)

    def test_a_bad_lane_name_is_refused(self):
        self.lane_task("a", "repo", lane="no spaces")
        with self.assertRaisesRegex(TaskError, "lane"):
            load_tasks(load_config(self.run_dir / "baton.toml"))

    def test_plan_shows_lanes_and_warns_when_nothing_can_overlap(self):
        self.lane_task("a", "repo")
        self.lane_task("b", "repo")
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(any("nothing runs in parallel" in w for w in warnings), warnings)
        text = plan_text(cfg, tasks)
        self.assertIn("lanes: up to 2 tasks at once", text)
        self.assertIn("lane repo", text)
        self.lane_task("b", "repo2")
        _, warnings, _ = preflight(cfg)
        self.assertFalse(any("nothing runs in parallel" in w for w in warnings), warnings)

    def test_plan_is_unchanged_with_one_lane(self):
        self.write_config()
        self.lane_task("a", "repo")
        cfg = load_config(self.run_dir / "baton.toml")
        _, warnings, tasks = preflight(cfg)
        self.assertNotIn("lane", plan_text(cfg, tasks))
        self.assertEqual([w for w in warnings if "lane" in w], [])


class LanesStateTest(LanesFixture):
    def test_a_version_1_state_file_loads_with_nothing_running(self):
        (self.run_dir / "state").mkdir(exist_ok=True)
        path = self.run_dir / "state" / "run.json"
        path.write_text(json.dumps({"current": "claude", "providers": {}, "quick_fails": 2,
                                    "tasks": {"a": {"status": "done", "attempts": 1}}}))
        st = load_state(path)
        self.assertEqual((st.running, st.quick_fails, st.tasks["a"].status), ({}, 2, "done"))

    def test_the_state_is_written_as_version_2_with_running(self):
        st = RunState(running={"a": {"lane": "repo", "provider": "claude", "attempt": 1, "out": "x"}})
        path = self.run_dir / "run.json"
        save_state(path, st)
        d = json.loads(path.read_text())
        self.assertEqual(d["version"], 2)
        self.assertEqual(load_state(path).running, st.running)

    def test_status_lists_the_running_lanes(self):
        import contextlib
        import io
        (self.run_dir / "state").mkdir(exist_ok=True)
        save_state(self.run_dir / "state" / "run.json", RunState(
            running={"a": {"lane": "repo", "provider": "claude", "attempt": 2, "out": ""}}))
        lock = (self.run_dir / "state" / "lock").open("w")        # a run is active when the lock is held
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["status", "-c", str(self.run_dir / "baton.toml")])
        finally:
            lock.close()
        self.assertIn("running now:", buf.getvalue())
        self.assertIn("[repo] a on claude, attempt 2", buf.getvalue())


class LanesWatchTest(LanesFixture):
    def test_watch_follows_the_named_lane_or_the_first_busy_one(self):
        from baton.watch import latest_attempt
        cfg = load_config(self.run_dir / "baton.toml")
        for tid, prov in (("a", "claude"), ("b", "codex")):
            d = cfg.results_dir / tid
            d.mkdir(parents=True)
            (d / f"attempt-01.{prov}.out").write_text("{}\n")
        (self.run_dir / "state").mkdir(exist_ok=True)
        save_state(cfg.state_dir / "run.json", RunState(
            tasks={"a": TaskState(status="running"), "b": TaskState(status="running")},
            running={"a": {"lane": "repo", "provider": "claude"}, "b": {"lane": "repo2", "provider": "codex"}}))
        self.assertEqual(latest_attempt(cfg).parent.name, "a")                  # the first busy lane
        self.assertEqual(latest_attempt(cfg, lane="repo2").parent.name, "b")
        self.assertIsNone(latest_attempt(cfg, lane="nope"))

    def test_the_cli_has_a_lane_option(self):
        out = subprocess.run(["python3", "-m", "baton", "watch", "--help"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent.parent)
        self.assertIn("--lane", out.stdout)


if __name__ == "__main__":
    unittest.main()
