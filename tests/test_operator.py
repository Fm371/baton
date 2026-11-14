"""Operator commands and visibility: mark, retry, providers reset, status, times, branches, lock info,
workspace discovery, reload between attempts, 'Needs from you'. Fake CLIs only."""

import contextlib
import fcntl
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from baton import cli, notverified, ops, results, timefmt, versioninfo
from baton.autocheck import schedule_preflight
from baton.config import load_config
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.state import load_state, save_state
from baton.tasks import load_tasks, order_tasks
from baton.watch import active_message
from tests.test_runner import RunnerFixture, claude_limit

UTC = timezone.utc


def capture(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args)
    return rc, out.getvalue(), err.getvalue()


class Fixture(RunnerFixture):
    @property
    def cfg_path(self):
        return str(self.run_dir / "baton.toml")

    def cfg(self):
        return load_config(self.cfg_path)

    def ordered(self, cfg=None):
        cfg = cfg or self.cfg()
        return order_tasks(schedule_preflight(cfg, load_tasks(cfg)))

    def hold_lock(self):
        """Pretend a run is active: hold the run lock for the length of the test."""
        d = self.run_dir / "state"
        d.mkdir(exist_ok=True)
        f = (d / "lock").open("a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)
        return f

    def saved(self):
        return load_state(self.run_dir / "state" / "run.json")


# ---------------------------------------------------------------- small pieces


class TimeFormatTest(unittest.TestCase):
    def test_iso_with_offset_and_relative(self):
        tz = ZoneInfo("Asia/Tehran")
        now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
        t = now + timedelta(hours=2, minutes=10)
        self.assertEqual(timefmt.when(t, tz, now), "2026-10-31 17:40 +03:30 (in 2h10m)")
        self.assertEqual(timefmt.when(now - timedelta(minutes=45), tz, now), "2026-10-31 14:45 +03:30 (45m ago)")
        self.assertEqual(timefmt.when(now + timedelta(days=3, hours=4), tz, now).split("(")[1], "in 3d4h)")
        self.assertIn("(now)", timefmt.when(now + timedelta(seconds=20), tz, now))

    def test_an_expired_limit_reads_available_now(self):
        tz = UTC
        now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
        self.assertEqual(timefmt.until(now - timedelta(minutes=1), tz, now), "available now")
        self.assertEqual(timefmt.until(None, tz, now), "available now")
        self.assertIn("(in 1h)", timefmt.until(now + timedelta(hours=1), tz, now))


class NotVerifiedBulletsTest(unittest.TestCase):
    def test_a_bullet_wrapped_over_lines_is_one_entry(self):
        text = ("- The auth service's token and JWKS endpoints are an `httpx.MockTransport` handler, not\n"
                "  the real server.\n"
                "- Redaction of uvicorn access-log lines is not\n"
                "  checked either.\n"
                "\n"
                "1. numbered\n   and wrapped\n")
        self.assertEqual(notverified.section_items(text), [
            "The auth service's token and JWKS endpoints are an `httpx.MockTransport` handler, not the real server.",
            "Redaction of uvicorn access-log lines is not checked either.",
            "numbered and wrapped"])

    def test_text_without_bullets_and_none_still_work(self):
        self.assertEqual(notverified.section_items("None."), [])
        self.assertEqual(notverified.section_items("plain text\nsecond line"), ["plain text second line"])


class ReasonLineTest(unittest.TestCase):
    def test_first_prose_line_after_status(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "t.md"
        p.write_text("STATUS: partial\n\n## Summary\n\n```\nnoise\n```\nThe suite is not green: test_provider fails "
                     "(T05).\nMore.\n")
        self.assertEqual(results.reason_line(p), "The suite is not green: test_provider fails (T05).")
        self.assertEqual(results.reason_line(p.parent / "nope.md"), "")


class VersionTest(unittest.TestCase):
    def test_compare_dotted_versions(self):
        self.assertTrue(versioninfo.older_than("0.3.0", "0.3.1"))
        self.assertFalse(versioninfo.older_than("0.10.0", "0.9.9"))
        self.assertFalse(versioninfo.older_than("0.3.0", "0.3.0"))
        self.assertEqual(versioninfo.parse("1.2.x"), (1, 2))


# ---------------------------------------------------------------- mark, retry, providers


class MarkTest(Fixture):
    def blocked_chain(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "blocked"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "blocked")
        self.assertEqual(self.state.tasks["b"].status, "skipped")

    def test_mark_done_records_the_override_and_frees_dependents(self):
        self.blocked_chain()
        rc, out, _ = capture(cli.main, ["mark", "a", "done", "--note", "the test, not the code", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn("a is now done", out)
        self.assertIn("b can run again", out)
        st = self.saved()
        self.assertEqual(st.tasks["a"].status, "done")
        self.assertEqual(st.tasks["a"].overridden_by, "operator")
        self.assertIn("the test, not the code", st.tasks["a"].note)
        self.assertEqual(st.tasks["b"].status, "pending")
        result = (self.run_dir / "results" / "a.md").read_text()
        self.assertTrue(result.startswith("STATUS: done"))
        self.assertTrue(any((self.run_dir / "results" / "a").glob("result-before-mark-*.md")))   # the old one is kept
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("[set by operator]", summary)
        self.assertIn("marked done by operator", (self.run_dir / "logs" / "run.log").read_text())

    def test_mark_with_a_result_file_copies_it(self):
        self.blocked_chain()
        mine = self.root / "mine.md"
        mine.write_text("Checked by hand: money gate passed.\n")
        rc, _, _ = capture(cli.main, ["mark", "a", "done", "--result", str(mine), "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        text = (self.run_dir / "results" / "a.md").read_text()
        self.assertEqual(text.splitlines()[0], "STATUS: done")
        self.assertIn("money gate passed", text)

    def test_mark_unknown_task_and_missing_result_are_refused(self):
        self.blocked_chain()
        rc, _, err = capture(cli.main, ["mark", "nope", "done", "-c", self.cfg_path])
        self.assertEqual(rc, 2)
        self.assertIn("no task with id 'nope'", err)
        rc, _, err = capture(cli.main, ["mark", "a", "done", "--result", str(self.root / "missing.md"), "-c", self.cfg_path])
        self.assertEqual(rc, 2)
        self.assertEqual(self.saved().tasks["a"].status, "blocked")

    def test_mark_while_a_run_is_active_is_queued_and_applied_between_tasks(self):
        self.blocked_chain()
        lock = self.hold_lock()
        rc, out, _ = capture(cli.main, ["mark", "a", "done", "--note", "queued", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn("queued", out)
        self.assertEqual(self.saved().tasks["a"].status, "blocked")        # nothing written by the CLI
        self.assertEqual(len(list((self.run_dir / "state" / "ops").glob("*.json"))), 1)
        lock.close()
        cfg = self.cfg()
        r = Runner(cfg, echo=False)                                     # stands in for the live runner
        r.ordered = self.ordered(cfg)
        r.apply_operator_ops()
        self.assertEqual(r.state.task("a").status, "done")
        self.assertEqual(r.state.task("b").status, "pending")
        self.assertEqual(list((self.run_dir / "state" / "ops").glob("*.json")), [])

    def test_a_task_that_is_running_right_now_is_not_marked(self):
        self.blocked_chain()
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        r.ordered = self.ordered(cfg)
        r.run_is_live = True
        r.state.task("a").status = "running"
        with self.assertRaises(ops.OpsError):
            ops.apply_mark(r, r.ordered[0], "done", "", None)


class RetryTest(Fixture):
    def test_retry_clears_the_task_and_runs_only_it(self):
        self.add_task("a")
        self.add_task("b")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "blocked"}, {"commit": "b.txt", "status": "done"},
                                 {"commit": "a2.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "blocked")
        self.assertEqual(self.state.tasks["b"].status, "done")
        rc, out, _ = capture(cli.main, ["retry", "a", "-c", self.cfg_path])
        self.assertEqual(rc, 0, out)
        self.assertIn("a cleared (it was blocked)", out)
        st = self.saved()
        self.assertEqual(st.tasks["a"].status, "done")
        self.assertEqual(st.tasks["b"].status, "done")
        self.assertEqual(len(self.calls()), 3)                          # b was not run again
        self.assertTrue(any((self.run_dir / "results" / "a").glob("earlier-*")))     # old attempt files kept

    def test_retry_refuses_a_checkpoint_and_a_task_behind_an_open_gate(self):
        (self.run_dir / "tasks" / "ok.md").write_text('+++\nkind = "approve"\n+++\nIs it right?\n')
        rc, _, err = capture(cli.main, ["retry", "ok", "-c", self.cfg_path])
        self.assertEqual(rc, 2)
        self.assertIn("approve", err)

    def test_retry_behind_an_open_checkpoint_is_refused_unless_forced(self):
        (self.run_dir / "tasks" / "ok.md").write_text('+++\nkind = "approve"\n+++\nIs it right?\n')
        self.add_task("b", deps=["ok"])
        cfg = self.cfg()
        st = load_state(cfg.state_dir / "run.json")
        st.task("ok").status = "awaiting"
        save_state(cfg.state_dir / "run.json", st)
        rc, out, _ = capture(cli.main, ["retry", "b", "-c", self.cfg_path])
        self.assertEqual(rc, 5)
        self.assertIn("gated by approval ok", out)
        self.scenario_is(claude=[{"commit": "b.txt", "status": "done"}])
        rc, _, _ = capture(cli.main, ["retry", "b", "--force", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertEqual(self.saved().tasks["b"].status, "done")

    def test_retry_while_a_run_is_active_is_queued_for_that_run(self):
        self.add_task("a")
        self.hold_lock()
        rc, out, _ = capture(cli.main, ["retry", "a", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn("queued", out)
        ops_dir = Path(self.cfg_path).parent / "state" / "ops"
        self.assertEqual([json.loads(p.read_text()) for p in ops_dir.glob("*.json")], [{"op": "retry", "task": "a"}])


class ProvidersResetTest(Fixture):
    def test_reset_clears_a_stale_limit(self):
        self.add_task("a")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)")] , codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIsNotNone(self.saved().providers["claude"]["limited_until"])
        rc, out, _ = capture(cli.main, ["providers", "reset", "claude", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn("cleared", out)
        self.assertIsNone(self.saved().providers["claude"]["limited_until"])

    def test_reset_while_a_run_is_active_is_queued(self):
        self.add_task("a")
        self.hold_lock()
        rc, out, _ = capture(cli.main, ["providers", "reset", "codex", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        self.assertIn("queued", out)
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        r.ordered = self.ordered(cfg)
        r.sched.disable("codex", "sandbox unusable")
        r.apply_operator_ops()
        self.assertIsNone(r.sched.status["codex"].disabled)


# ---------------------------------------------------------------- status and summary


class StatusTest(Fixture):
    def test_status_is_rebuilt_from_the_state_and_says_what_to_do(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "result_extra": "\nSuite is not green: x fails.\n"},
                                 {"commit": "b.txt", "status": "partial", "result_extra": "\nOnly half of it works.\n"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.saved()
        st.tasks["a"].status = "failed"             # edited by hand: SUMMARY.md on disk is now stale
        save_state(self.run_dir / "state" / "run.json", st)
        rc, out, _ = capture(cli.main, ["status", "-c", self.cfg_path])
        self.assertEqual(rc, 0)
        row = next(ln for ln in out.splitlines() if ln.startswith("| a "))
        self.assertIn("| failed |", row)
        self.assertIn("what to do next", out)
        self.assertIn("baton retry a", out)
        self.assertIn('baton mark a done --note', out)

    def test_the_note_column_carries_the_reason_from_the_result_file(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial",
                                  "result_extra": "\n## Issues\nThe full suite is not green because of T05.\n"}])
        self.assertEqual(self.run_baton(), 0)
        row = next(ln for ln in (self.run_dir / "results" / "SUMMARY.md").read_text().splitlines()
                   if ln.startswith("| a "))
        self.assertIn("The full suite is not green because of T05.", row)

    def test_a_task_in_the_middle_of_an_attempt_when_the_run_ended_is_interrupted(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        r.ordered = self.ordered(cfg)
        r.state.task("a").status = "running"
        self.assertIn("| interrupted |", r.summary_text(r.ordered, ended=True))
        self.assertIn("| running |", r.summary_text(r.ordered, ended=False))
        st = r.state
        save_state(cfg.state_dir / "run.json", st)
        self.assertIn("[interrupted]", plan_text(cfg, self.ordered(cfg)))

    def test_summary_first_line_has_the_baton_version(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        first = (self.run_dir / "results" / "SUMMARY.md").read_text().splitlines()[0]
        self.assertIn("baton " + versioninfo.describe(), first)
        self.assertEqual(self.saved().baton_version, versioninfo.describe())

    def test_needs_from_you_is_gathered_at_the_top(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done",
                                  "result_extra": "\n## Needs from you\n- set PUBLIC_BASE_URL on the server\n"
                                                  "- run the 0007 migration\n"}])
        self.assertEqual(self.run_baton(), 0)
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("## Needs from you", summary)
        self.assertLess(summary.index("## Needs from you"), summary.index("| task |"))
        self.assertIn("**a**: set PUBLIC_BASE_URL on the server", summary)
        self.assertIn("Needs from you", self.calls()[0]["prompt"])

    def test_expired_limits_are_not_shown_in_summary_or_plan(self):
        self.add_task("a")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        cfg = self.cfg()
        late = datetime(2027, 1, 1, tzinfo=UTC)
        text = plan_text(cfg, self.ordered(cfg), now=late)
        self.assertNotIn("limited until", text)           # dropped when the state is loaded, not shown as past
        self.assertNotIn("its limit has passed", text)

    def test_branches_are_listed_and_conflicts_reported(self):
        self.add_task("a", extra='')
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/a"\n+++\nBuild a.\n')
        (self.run_dir / "tasks" / "b.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/b"\n+++\nBuild b.\n')
        self.scenario_is(claude=[{"commit": "same.txt", "content": "from a\n", "status": "done"},
                                 {"commit": "same.txt", "content": "from b\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        report = (self.run_dir / "results" / "BRANCHES.md").read_text()
        self.assertIn("`feat/a`", report)
        self.assertIn("`feat/b`", report)
        self.assertIn("conflict** in `same.txt`", report)
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("## Branches", summary)
        self.assertIn("| a | repo | `feat/a` |", summary)
        self.assertIn("conflicting pair", self.log)


# ---------------------------------------------------------------- lock, workspace, reload


class LockAndWorkspaceTest(Fixture):
    def test_the_lock_names_the_run_that_holds_it(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "sleep": 0.2}])
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        seen = {}

        real = r.run_task

        def spy(task, provider=None):
            seen["info"] = json.loads((cfg.state_dir / "lock").read_text())
            seen["msg"] = active_message(cfg)
            return real(task, provider)

        r.run_task = spy
        self.assertEqual(r.run(preflight(cfg)[2]), 0)
        self.assertEqual(seen["info"]["pid"], os.getpid())
        self.assertIn("run", seen["info"])
        self.assertIn(f"pid {os.getpid()}", seen["msg"])
        self.assertIn("baton status", seen["msg"])

    def test_a_second_run_does_not_erase_the_first_ones_lock_info(self):
        d = self.run_dir / "state"
        d.mkdir()
        (d / "lock").write_text(json.dumps({"pid": os.getpid(), "started": "now", "run": "x"}))
        self.hold_lock()
        self.add_task("a")
        cfg = self.cfg()
        self.assertEqual(Runner(cfg, echo=False).run([]), 4)
        self.assertEqual(json.loads((d / "lock").read_text())["pid"], os.getpid())

    def test_config_is_found_in_a_parent_folder_and_by_env(self):
        sub = self.run_dir / "tasks"
        old = os.getcwd()
        os.chdir(sub)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BATON_WORKSPACE", None)
            self.assertEqual(cli.find_config(None).resolve(), (self.run_dir / "baton.toml").resolve())
        os.chdir(self.root)
        with mock.patch.dict(os.environ, {"BATON_WORKSPACE": str(self.run_dir)}):
            self.assertEqual(cli.find_config(None), self.run_dir / "baton.toml")
        self.assertEqual(cli.find_config("x.toml"), Path("x.toml"))

    def test_no_config_anywhere_says_where_to_run_baton(self):
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BATON_WORKSPACE", None)
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    cli._cfg(mock.Mock(config=None))
        self.assertIn("BATON_WORKSPACE", err.getvalue())
        self.assertIn("workspace folder", err.getvalue())

    def test_a_closed_pipe_is_not_an_error(self):
        with mock.patch.object(cli, "cmd_status", side_effect=BrokenPipeError), \
                mock.patch("baton.cli.os.dup2") as dup2:
            self.assertEqual(cli.main(["status", "-c", self.cfg_path]), 0)
        self.assertTrue(dup2.called)


class ReloadTest(Fixture):
    def test_task_file_and_baton_toml_edits_apply_before_the_next_attempt(self):
        self.add_task("a")
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        task = self.ordered(cfg)[0]
        r.ordered = [task]
        r.cfg.max_quick_unknown = 1                   # set in memory, not in the file: must survive a reload
        self.add_task("a", extra="timeout_min = 30\npriority = 4\n")
        text = Path(self.cfg_path).read_text().replace("[run]\n", "[run]\ntask_timeout_min = 100\n", 1)
        Path(self.cfg_path).write_text(text)
        r.reload_for_attempt(task)
        self.assertEqual((task.timeout_min, task.priority), (30, 4))
        self.assertEqual(r.cfg.task_timeout_min, 100)
        self.assertEqual(r.cfg.max_quick_unknown, 1)
        log = (self.run_dir / "logs" / "run.log").read_text()
        self.assertIn("changed since the last attempt", log)
        self.assertIn("timeout_min None -> 30", log)
        self.assertIn("task_timeout_min", log)
        r.reload_for_attempt(task)                    # nothing new: nothing logged again
        self.assertEqual((self.run_dir / "logs" / "run.log").read_text().count("changed since"), 1)

    def test_a_broken_task_file_is_ignored_and_reported_once(self):
        self.add_task("a")
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        task = self.ordered(cfg)[0]
        (self.run_dir / "tasks" / "a.md").write_text("+++\nrepo = \n+++\nBuild a.\n")
        r.reload_for_attempt(task)
        r.reload_for_attempt(task)
        log = (self.run_dir / "logs" / "run.log").read_text()
        self.assertEqual(log.count("not reloaded"), 1)
        self.assertEqual(task.work_type, "feature")


class PlanTest(Fixture):
    def test_plan_warns_when_baton_is_older_than_min_version(self):
        self.write_config(min_version="99.0.0")
        self.add_task("a")
        _, warnings, _ = preflight(self.cfg())
        self.assertTrue(any("min_version = 99.0.0" in w for w in warnings))

    def test_the_first_command_uses_the_provider_the_first_task_may_use(self):
        self.add_task("a", extra='providers = ["codex"]\n')
        cfg = self.cfg()
        text = plan_text(cfg, self.ordered(cfg))
        first = text.split("first command")[1]
        self.assertIn("/bin/codex", first)

    def test_approve_message_says_when_a_decision_is_not_due_yet(self):
        (self.run_dir / "tasks" / "ok.md").write_text('+++\nkind = "approve"\n+++\nIs it right?\n')
        rc, _, err = capture(cli.main, ["approve", "ok", "-c", self.cfg_path])
        self.assertEqual(rc, 2)
        self.assertIn("has not asked for this decision yet", err)
        self.assertIn("baton run", err)


if __name__ == "__main__":
    unittest.main()
