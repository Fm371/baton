"""Classifier false positives, bounded reads of huge output, a damaged run.json, a full disk, a stalled
agent, the cap on limits that change nothing, and a wall clock stepped back during an attempt."""

import errno
import json
import tempfile
import time
import tracemalloc
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from baton.config import ConfigError, load_config
from baton.limits import classify_claude, classify_codex, read_output
from baton.preflight import free_space_warning
from baton.runner import Runner
from baton.state import DiskFull, RunState, StateError, atomic_write, load_state, save_state
from baton.watch import BACKLOG_BYTES, open_at_backlog
from baton.preflight import preflight
from tests.test_lanes import LanesFixture
from tests.test_runner import FakeClock, claude_limit, codex_limit, git
from tests.test_signals import SignalFixture

NOW = datetime(2026, 10, 24, 18, 30, tzinfo=timezone.utc)


def jl(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FalsePositiveTest(unittest.TestCase):
    def test_a_line_number_in_a_stack_trace_is_not_an_auth_error(self):
        err = "TypeError: x is not a function\n    at run (/usr/lib/cli.js:401:9)\n    at main (cli.js:503:1)"
        for fn in (classify_claude, classify_codex):
            oc = fn("", err, 1, NOW, timezone.utc)
            self.assertEqual(oc.kind, "unknown", (fn.__name__, oc))

    def test_status_wording_still_counts(self):
        self.assertEqual(classify_claude("", "request failed: HTTP 401", 1, NOW, timezone.utc).kind, "auth")
        self.assertEqual(classify_claude("", "API Error: 529 Overloaded", 1, NOW, timezone.utc).kind, "transient")
        self.assertEqual(classify_codex("", "error: 503 Service Unavailable", 1, NOW, timezone.utc).kind,
                         "transient")
        self.assertEqual(classify_codex("", "Selected model is at capacity", 1, NOW, timezone.utc).kind,
                         "transient")

    def test_the_word_capacity_alone_is_nothing(self):
        oc = classify_claude("", "capacity planning doc updated; line 503", 1, NOW, timezone.utc)
        self.assertEqual(oc.kind, "unknown")

    def test_events_outrank_stderr(self):
        # the CLI said "not logged in" in its own result; a rate-limit word in stderr debug output must not win
        out = jl({"type": "result", "is_error": True, "result": "Not logged in. Please run /login"})
        oc = classify_claude(out, "debug: rate limit window refreshed", 1, NOW, timezone.utc)
        self.assertEqual(oc.kind, "auth")

    def test_error_max_turns_ignores_the_agents_own_last_words(self):
        out = jl({"type": "result", "subtype": "error_max_turns", "is_error": True,
                  "result": "I handled the usage limit reached case and the 429 retry in the code"})
        oc = classify_claude(out, "", 1, NOW, timezone.utc)
        self.assertEqual(oc.kind, "unknown")

    def test_an_error_result_message_is_still_read(self):
        out = jl({"type": "result", "subtype": "success", "is_error": True,
                  "result": "You've hit your session limit · resets 10:10pm (Asia/Tehran)"})
        self.assertEqual(classify_claude(out, "", 1, NOW, timezone.utc).kind, "limit")


class BoundedReadTest(unittest.TestCase):
    def test_a_huge_output_is_read_in_bounded_memory(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.out"
            line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"' + b"x" * 900 + b'"}]}}\n'
            with p.open("wb") as f:
                f.write(b'{"type":"system","subtype":"init","session_id":"s-1"}\n')
                for _ in range(110_000):                    # about 100 MB
                    f.write(line)
                f.write(b'{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1792837800}}\n')
                f.write(b'{"type":"result","is_error":true,"result":"You\'ve hit your session limit"}\n')
            self.assertGreater(p.stat().st_size, 100_000_000)
            tracemalloc.start()
            try:
                text = read_output(p)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(len(text), 9_000_000)
            self.assertLess(peak, 40_000_000)               # never near the file's size
            self.assertIn('"session_id":"s-1"', text)       # the session id line from the start is kept
            oc = classify_claude(text, "", 1, NOW, timezone.utc)
            self.assertEqual(oc.kind, "limit")
            self.assertEqual(oc.session_id, "s-1")

    def test_a_small_file_is_read_whole(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.out"
            p.write_text("one\ntwo\n")
            self.assertEqual(read_output(p), "one\ntwo\n")
            self.assertEqual(read_output(Path(d) / "missing"), "")

    def test_watch_starts_near_the_end_of_a_huge_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.out"
            with p.open("w") as f:
                for i in range(60_000):
                    f.write(f"line {i:06d} " + "y" * 90 + "\n")
            f2 = open_at_backlog(p)
            try:
                rest = f2.read()
            finally:
                f2.close()
            self.assertLessEqual(len(rest), BACKLOG_BYTES)
            self.assertTrue(rest.startswith("line "))          # on a whole line
            self.assertTrue(rest.rstrip().endswith("y" * 90))


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.path = self.d / "state" / "run.json"

    def test_the_previous_good_save_is_kept_as_a_backup(self):
        st = RunState()
        st.task("a").status = "done"
        save_state(self.path, st)
        st.task("b").status = "running"
        save_state(self.path, st)
        bak = json.loads((self.path.parent / "run.json.bak").read_text())
        self.assertEqual(set(bak["tasks"]), {"a"})           # the save before the last one

    def test_a_damaged_run_json_falls_back_to_the_backup(self):
        st = RunState()
        st.task("a").status = "done"
        save_state(self.path, st)
        save_state(self.path, st)
        self.path.write_text('{"version": 2, "tasks": {"a": ')       # cut off, as after a power loss
        got = load_state(self.path)
        self.assertEqual(got.tasks["a"].status, "done")
        self.assertEqual(got.recovered_from, "run.json.bak")
        save_state(self.path, got)                                   # a damaged file never overwrites the backup
        self.assertEqual(load_state(self.path.parent / "run.json.bak").tasks["a"].status, "done")

    def test_no_backup_gives_a_clear_error_not_a_traceback(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(StateError) as cm:
            load_state(self.path)
        msg = str(cm.exception)
        self.assertIn("run.json", msg)
        self.assertIn("mv ", msg)                                   # says what to do

    def test_the_cli_reports_a_damaged_state_with_exit_2(self):
        from baton import cli
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with mock.patch("baton.cli.cmd_plan", side_effect=lambda a: load_state(self.path)), \
                mock.patch("sys.stderr"):
            rc = cli.main(["plan"])
        self.assertEqual(rc, 2)

    def test_a_full_disk_is_a_diskfull_error(self):
        with mock.patch("baton.state.tempfile.mkstemp", side_effect=OSError(errno.ENOSPC, "No space left")):
            with self.assertRaises(DiskFull) as cm:
                atomic_write(self.path, "x")
        self.assertIn(str(self.path.parent), str(cm.exception))


class ConfigKeysTest(unittest.TestCase):
    def load(self, run: str):
        d = Path(tempfile.mkdtemp())
        (d / "baton.toml").write_text(
            f"[run]\n{run}\n[providers.claude]\nbin = \"claude\"\n"
            "[routing.rungs.r1]\nclaude = { model = \"m\" }\n[routing.work_types]\nx = \"r1\"\n")
        return load_config(d / "baton.toml")

    def test_defaults(self):
        cfg = self.load("")
        self.assertEqual((cfg.min_free_mb, cfg.stall_minutes, cfg.max_limit_cycles), (500, 0, 6))

    def test_values_are_checked(self):
        for bad in ('stall_minutes = "5"', "min_free_mb = -1", "max_limit_cycles = 0", "max_limit_cycles = 1.5"):
            with self.assertRaises(ConfigError, msg=bad):
                self.load(bad)
        cfg = self.load("stall_minutes = 20\nmin_free_mb = 100\nmax_limit_cycles = 3")
        self.assertEqual((cfg.min_free_mb, cfg.stall_minutes, cfg.max_limit_cycles), (100, 20, 3))

    def test_free_space_warning(self):
        cfg = self.load("min_free_mb = 1")
        self.assertIsNone(free_space_warning(cfg))
        cfg.min_free_mb = 10 ** 12                                   # more than any disk has
        self.assertIn("free", free_space_warning(cfg))
        cfg.min_free_mb = 0
        self.assertIsNone(free_space_warning(cfg))


class RunsTest(SignalFixture):
    def test_a_full_disk_stops_the_run_cleanly_and_keeps_run_json(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"touch": str(flag), "dirty": "x.txt", "status": "done", "commit": "w"}])
        real = tempfile.mkstemp

        def mkstemp(*a, **kw):                  # the disk fills up once the agent has started
            if flag.exists():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*a, **kw)
        r, tasks = self.runner()
        with mock.patch("baton.state.tempfile.mkstemp", mkstemp):
            rc = r.run(tasks)
        self.assertEqual(rc, 6)
        state = json.loads((r.cfg.state_dir / "run.json").read_text())      # still valid JSON
        self.assertIn("a", state["tasks"])
        self.assertIsNotNone(r.disk_full)
        # freeing space and running again continues
        self.scenario_is(claude=[{"dirty": "y.txt", "status": "done", "commit": "w2"}])
        rc2 = Runner(load_config(self.run_dir / "baton.toml"), clock=FakeClock(), echo=False).run()
        self.assertEqual(rc2, 0)

    def test_a_stalled_agent_is_ended_and_retried_once(self):
        self.add_task("a")
        self.write_config(stall_minutes=0.02, quick_exit_seconds=0)         # 1.2 s of silence
        self.scenario_is(claude=[
            {"stdout": [{"type": "assistant", "message": {"content": [{"type": "text", "text": "waiting for db"}]}}],
             "dirty": "wip.txt", "hang": 60},
            {"dirty": "z.txt", "status": "done", "commit": "finished"},
        ])
        r, tasks = self.runner()
        t0 = time.time()
        rc = self.finish_run(r, tasks)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("attempt 1 stalled", self.log)
        self.assertNotIn("no limit pattern matched", self.log)               # baton ended it itself
        prompts = [c["prompt"] for c in self.calls()]
        self.assertIn("stalled", prompts[1])
        self.assertIn("waiting for db", prompts[1])                          # where it stopped
        self.assertIn("wip", git(self.repo, "log", "--format=%s", "feat/x"))

    def test_an_agent_that_stalls_twice_fails_the_task(self):
        self.add_task("a")
        self.write_config(stall_minutes=0.02, quick_exit_seconds=0)
        self.scenario_is(claude=[{"hang": 60}, {"hang": 60}])
        r, tasks = self.runner()
        rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertIn("stalled again", self.state.tasks["a"].note)

    def test_stall_minutes_in_the_task_file_overrides_the_config(self):
        self.add_task("a", extra="stall_minutes = 0.02\n")
        self.scenario_is(claude=[{"hang": 60}, {"dirty": "z.txt", "status": "done", "commit": "ok"}])
        r, tasks = self.runner()
        rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "done")

    def test_limits_that_change_nothing_stop_after_the_cap(self):
        self.add_task("a")
        self.write_config(max_limit_cycles=2, quick_exit_seconds=0)
        self.scenario_is(claude=[claude_limit()] * 4, codex=[codex_limit()] * 4)
        r, tasks = self.runner()
        rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        ts = self.state.tasks["a"]
        self.assertEqual(ts.status, "failed")
        self.assertIn("usage-limit outcomes in a row", ts.note)
        self.assertEqual(len(self.calls()), 2)                              # not an endless bounce

    def test_a_limit_after_progress_starts_the_count_over(self):
        self.add_task("a")
        self.write_config(max_limit_cycles=2, quick_exit_seconds=0)
        c1 = {**claude_limit(), "commit": "first.txt"}
        x1 = {**codex_limit(), "commit": "second.txt"}
        self.scenario_is(claude=[c1, {"dirty": "z.txt", "status": "done", "commit": "end"}], codex=[x1])
        r, tasks = self.runner()
        rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["a"].limit_cycles, 1)

    def test_a_clock_stepped_back_during_the_attempt_does_not_hide_the_result(self):
        self.add_task("a")
        flag = self.root / "started"
        self.scenario_is(claude=[{"touch_start": str(flag), "sleep": 1.5, "dirty": "q.txt", "status": "done",
                                  "commit": "late"}])
        real = time.time

        def fake_time():
            # before the agent starts the wall clock reads 100 s ahead; once it runs it is correct again,
            # as after an NTP step back: the result file's mtime is older than the recorded start
            return real() if flag.exists() else real() + 100
        r, tasks = self.runner()
        with mock.patch.object(time, "time", fake_time):
            rc = self.finish_run(r, tasks)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("clock moved back", self.log)


class LanesDiskFullTest(LanesFixture):
    def test_a_full_disk_stops_every_lane_with_exit_6(self):
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", providers=["codex"])
        up = self.root / "up"
        self.scenario_is(
            claude=[self.step("a", touch_start=str(up), sleep=1.0)],
            codex=[self.step("b", touch_start=str(self.root / "up2"), sleep=1.0)])
        real = tempfile.mkstemp

        def mkstemp(*a, **kw):                  # the disk fills once the agents have started
            if up.exists():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*a, **kw)
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        r = Runner(cfg, clock=FakeClock(), echo=False)
        t0 = time.time()
        with mock.patch("baton.state.tempfile.mkstemp", mkstemp):
            rc = r.run(tasks)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(rc, 6)
        json.loads((cfg.state_dir / "run.json").read_text())             # intact


if __name__ == "__main__":
    unittest.main()
