"""End-to-end runs with real git repos, fake CLIs and a fake clock."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from baton.clock import Clock
from baton.config import load_config
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.state import load_state

HERE = Path(__file__).parent
T0 = datetime(2026, 10, 24, 18, 30, tzinfo=timezone.utc)   # 22:00 in Tehran


class FakeClock(Clock):
    def __init__(self, start=T0, step=timedelta(seconds=1)):
        self.t = start
        self.step = step
        self.slept = []

    def now(self):
        self.t += self.step
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)

    def sleep_until(self, when, should_stop=lambda: False):
        self.slept.append(when)
        self.t = max(self.t, when)
        return True


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def claude_limit(reset="10:40pm (Asia/Tehran)", window="session"):
    return {"exit": 1, "stdout": [
        {"type": "system", "subtype": "init", "session_id": "c-1"},
        {"type": "result", "is_error": True, "result": f"You've hit your {window} limit · resets {reset}"}]}


def codex_limit(when="11:30 PM"):
    msg = f"You've hit your usage limit. Upgrade to Pro or try again at {when}."
    return {"exit": 1, "stdout": [{"type": "thread.started", "thread_id": "x-1"},
                                  {"type": "error", "message": msg},
                                  {"type": "turn.failed", "error": {"message": msg}}]}


class RunnerFixture(unittest.TestCase):
    """A workspace with a real git repo, fake CLIs and helpers; no tests of its own."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "develop")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "README").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.run_dir = self.root / "task-auto"
        (self.run_dir / "tasks").mkdir(parents=True)
        (self.run_dir / "common.md").write_text("COMMON RULES\n")
        bins = self.root / "bin"
        bins.mkdir()
        for name in ("claude", "codex"):
            p = bins / name
            p.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_agent.py'} {name} \"$@\"\n")
            p.chmod(0o755)
        home = self.root / "empty-home"
        home.mkdir()
        (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "sol"}]}))
        self.scenario = self.root / "scenario.json"
        os.environ["BATON_FAKE_SCENARIO"] = str(self.scenario)
        self.write_config()

    def write_config(self, extra="", **run):
        # The scenarios were written for a Claude-first run; the default itself is
        # covered in test_default_start_provider.
        opts = {"start_provider": "claude", "quick_exit_seconds": 0, "transient_wait_seconds": 5, "timezone": "Asia/Tehran",
                "default_base": "develop", "common_prompt": "common.md", **run}
        run_toml = "\n".join(f"{k} = {json.dumps(v)}" for k, v in opts.items())
        empty = self.root / "empty-home"
        (self.run_dir / "baton.toml").write_text(f"""
[run]
{run_toml}

[providers.claude]
bin = "{self.root}/bin/claude"
home = "{empty}"
[providers.codex]
bin = "{self.root}/bin/codex"
home = "{empty}"

[routing.rungs.r3]
claude = {{ model = "sonnet", effort = "medium" }}
codex = {{ model = "sol", effort = "medium" }}
[routing.work_types]
feature = "r3"
{extra}
""")

    def add_task(self, tid, deps=(), extra=""):
        dep = f"depends_on = {json.dumps(list(deps))}\n" if deps else ""
        (self.run_dir / "tasks" / f"{tid}.md").write_text(
            f'+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\n{dep}{extra}+++\nBuild {tid}.\n')

    def add_verifier(self, vid, deps=("a",), body="SECRET CANARY: check rounding.", extra=""):
        d = self.run_dir / "verifiers"
        d.mkdir(exist_ok=True)
        (d / f"{vid}.md").write_text(
            f'+++\nkind = "verify"\nrepo = "../repo"\nwork_type = "feature"\n'
            f'depends_on = {json.dumps(list(deps))}\n{extra}+++\n{body}\n')

    def scenario_is(self, **steps):
        self.scenario.write_text(json.dumps(steps))

    def calls(self):
        p = Path(str(self.scenario) + ".calls.jsonl")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def run_baton(self, clock=None, only=None, force=False, provider=None):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg, provider)
        self.assertEqual(errors, [])
        self.clock = clock or FakeClock()
        r = Runner(cfg, clock=self.clock, only=only, echo=False, force=force, provider=provider)
        rc = r.run(tasks)
        self.state = load_state(cfg.state_dir / "run.json")
        self.log = (cfg.logs_dir / "run.log").read_text()
        self.metrics = [json.loads(x) for x in (cfg.results_dir / "metrics.jsonl").read_text().splitlines()]
        return rc



class RunnerTest(RunnerFixture):
    # ------------------------------------------------------------------

    def test_happy_path(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")      # put back when the run ended
        self.assertTrue(git(self.repo, "rev-parse", "--verify", "-q", "feat/x"))
        self.assertEqual(git(self.repo, "rev-list", "--count", "develop..feat/x"), "2")
        c = self.calls()[0]
        self.assertIn("COMMON RULES", c["prompt"])
        self.assertIn("Build a.", c["prompt"])
        self.assertEqual(c["argv"][:2], ["-p", "--output-format"])
        self.assertIn("--effort", c["argv"])
        self.assertNotIn("Do not run `git commit`", c["prompt"])
        self.assertEqual(Path(c["cwd"]).resolve(), self.repo.resolve())
        self.assertTrue((self.run_dir / "results" / "SUMMARY.md").is_file())

    def test_codex_changes_are_committed_with_result_message(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{
            "dirty": "a.txt", "status": "done",
            "commit_message": "fix(api): handle empty response",
        }])

        self.assertEqual(self.run_baton(), 0)

        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"),
                         "fix(api): handle empty response")
        call = self.calls()[0]
        self.assertIn("Do not run `git commit`", call["prompt"])
        self.assertIn("COMMIT: <conventional commit message>", call["prompt"])

    def test_codex_gets_results_and_configured_add_dirs(self):
        self.write_config(start_provider="codex")
        extra = self.run_dir / "extra"
        extra.mkdir()
        config_path = self.run_dir / "baton.toml"
        config_path.write_text(config_path.read_text().replace(
            "[providers.claude]\n", '[providers.claude]\nadd_dirs = ["extra"]\n'))
        self.add_task("a")
        self.scenario_is(codex=[{"status": "done"}])

        self.assertEqual(self.run_baton(), 0)

        argv = self.calls()[0]["argv"]
        mounted = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]
        self.assertEqual(mounted, [str(self.run_dir / "results" / "a"), str(extra)])

    def test_codex_status_falls_back_to_last_agent_message(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{
            "dirty": "a.txt",
            "stdout": [
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": "STATUS: blocked"}},
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": "Finished.\nSTATUS: done\n"}},
            ],
        }])

        self.assertEqual(self.run_baton(), 0)

        # weak evidence: no result file, so "done" is downgraded to partial
        self.assertEqual(self.state.tasks["a"].status, "partial")
        self.assertIn("no result file; STATUS taken from Codex's last message", self.state.tasks["a"].note)
        self.assertFalse((self.run_dir / "results" / "a.md").exists())
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"),
                         "chore(a): wip, left uncommitted at the end of the task (unverified)")
        self.assertEqual(self.metrics[-1]["result"], "partial")

    def test_codex_blocked_from_last_message_is_kept_as_blocked(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "stdout": [self.codex_message("STATUS: blocked")]}])
        self.run_baton()
        self.assertEqual(self.state.tasks["a"].status, "blocked")

    def codex_message(self, text):
        return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}

    def test_codex_implementer_add_dirs_skip_hidden_paths(self):
        self.write_config(start_provider="codex")
        config_path = self.run_dir / "baton.toml"
        config_path.write_text(config_path.read_text().replace(
            "[providers.codex]\n", '[providers.codex]\nadd_dirs = ["results/va", "extra"]\n'))
        (self.run_dir / "extra").mkdir()
        (self.run_dir / "results" / "va").mkdir(parents=True)
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}] * 2)
        self.run_baton()
        argv = [c for c in self.calls() if c["provider"] == "codex"][0]["argv"]
        mounted = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]
        self.assertEqual(mounted, [str(self.run_dir / "results" / "a"), str(self.run_dir / "extra")])

    def test_codex_agent_message_used_when_result_file_has_no_status(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        (self.run_dir / "results").mkdir()
        (self.run_dir / "results" / "a.md").write_text("notes only\n")
        self.scenario_is(codex=[{"dirty": "a.txt", "stdout": [self.codex_message("STATUS: done")]}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "partial")  # weak evidence

    def test_codex_agent_message_used_when_result_file_is_stale(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        old = self.run_dir / "results" / "a.md"
        old.parent.mkdir()
        old.write_text("STATUS: blocked\nCOMMIT: stale message\n")
        os.utime(old, (1, 1))
        self.scenario_is(codex=[{"dirty": "a.txt", "stdout": [self.codex_message("STATUS: done")]}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "partial")  # weak evidence
        self.assertNotIn("stale message", git(self.repo, "log", "-1", "--format=%s", "feat/x"))

    def test_codex_agent_message_status_ignored_after_error_exit(self):
        self.write_config(start_provider="codex", max_quick_unknown=1)
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "exit": 1, "stderr": "boom",
                                 "stdout": [self.codex_message("STATUS: done")]}])
        self.run_baton()
        self.assertNotEqual(self.state.tasks["a"].status, "done")
        self.assertIsNone(self.metrics[0]["result"])

    def test_codex_done_without_commit_line_is_not_called_unfinished(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        subject = git(self.repo, "log", "-1", "--format=%s", "feat/x")
        self.assertNotIn("unfinished", subject)
        self.assertNotIn("unverified", subject)

    def test_codex_partial_without_commit_line_stays_wip(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "partial"}])
        self.run_baton()
        self.assertIn("wip, ", git(self.repo, "log", "-1", "--format=%s", "feat/x"))

    def test_codex_done_with_empty_diff_makes_no_commit(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"status": "done", "commit_message": "feat: nothing"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "init")
        self.assertIn("no commits", self.state.tasks["a"].note)

    def test_codex_commit_message_uses_only_the_commit_line(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "done",
                                 "commit_message": "feat(a): add a\nCOMMIT: second\nbody line"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%B", "feat/x"), "feat(a): add a")

    def test_claude_limit_hands_over_to_codex_and_stays(self):
        self.add_task("a")
        self.add_task("b")
        self.scenario_is(
            claude=[{**claude_limit(), "dirty": "half.txt"}],
            codex=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        a = self.state.tasks["a"]
        self.assertEqual((a.status, a.providers), ("done", ["claude", "codex"]))
        # the half-done Claude work was committed as WIP, not lost or stashed
        log = git(self.repo, "log", "--format=%s", "develop..feat/x")
        self.assertIn("chore(a): wip, claude hit its usage limit (unverified)", log)
        # codex was told to continue
        codex_call = [c for c in self.calls() if c["provider"] == "codex"][0]
        self.assertIn("git log --stat", codex_call["prompt"])
        self.assertIn("claude hit its usage limit", codex_call["prompt"])
        self.assertEqual(codex_call["argv"][:2], ["exec", "--json"])
        self.assertIn("model_reasoning_effort=medium", codex_call["argv"])
        # sticky: task b ran on codex although claude reset at 22:40 meanwhile? (no sleep needed)
        self.assertEqual(self.state.tasks["b"].providers, ["codex"])
        self.assertIn("switch claude -> codex", self.log)
        m = [x for x in self.metrics if x["provider"] == "codex"][0]
        self.assertEqual(m["switched_from"], "claude")

    def test_both_limited_sleeps_until_earliest_reset(self):
        self.add_task("a")
        # claude resets 23:59 Tehran, codex 22:45 Tehran -> wait for codex
        self.scenario_is(claude=[claude_limit("11:59pm (Asia/Tehran)")],
                         codex=[codex_limit("10:45 PM"), {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        waits = [w for w in self.clock.slept if isinstance(w, datetime)]
        self.assertEqual(len(waits), 1)
        expected = datetime(2026, 10, 24, 19, 15, tzinfo=timezone.utc) + timedelta(seconds=180)
        self.assertEqual(waits[0], expected)
        self.assertIn("task a: all providers limited, sleeping until", self.log)
        self.assertIn("resuming after reset on codex", self.log)

    def summary_row(self, text, tid):
        return [line for line in text.splitlines() if line.startswith(f"| {tid} ")][0]

    def test_summary_shows_running_attempt_and_switch(self):
        self.add_task("a")
        self.add_task("b")
        snaps = self.root / "snaps"
        snaps.mkdir()
        self.scenario_is(
            claude=[{**claude_limit(), "snapshot": str(snaps / "1.md")}],
            codex=[{"commit": "a.txt", "status": "done", "snapshot": str(snaps / "2.md")}, {"status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        first = (snaps / "1.md").read_text()
        row = self.summary_row(first, "a")
        self.assertIn("| running | 0 | 1 | claude |", row)
        self.assertIn("attempt 1 on claude since", row)
        self.assertIn("| pending | 0 | 0 |", self.summary_row(first, "b"))
        second = (snaps / "2.md").read_text()
        row = self.summary_row(second, "a")
        self.assertIn("| running | 0 | 2 | claude -> codex |", row)
        self.assertIn("attempt 2 on codex since", row)
        self.assertIn("(switched from claude)", row)
        self.assertIn("- claude: limited until", second)
        self.assertIn("- codex: available (current)", second)

    def test_summary_shows_sleep_and_resume(self):
        self.add_task("a")
        snaps = self.root / "snaps"
        snaps.mkdir()
        summary = self.run_dir / "results" / "SUMMARY.md"
        seen = []

        class SnapClock(FakeClock):
            def sleep_until(clock, when, should_stop=lambda: False):
                seen.append(summary.read_text())
                return super().sleep_until(when, should_stop)

        self.scenario_is(claude=[claude_limit("11:59pm (Asia/Tehran)")],
                         codex=[codex_limit("10:45 PM"),
                                {"commit": "a.txt", "status": "done", "snapshot": str(snaps / "3.md")}])
        self.assertEqual(self.run_baton(clock=SnapClock()), 0)
        row = self.summary_row(seen[0], "a")
        self.assertIn("| running | 0 | 2 | claude -> codex |", row)
        self.assertIn("sleeping until 2026-10-24 22:48 +03:30 (in 47m)", row)
        self.assertIn("- codex: limited until 2026-10-24 22:48 +03:30 (in 47m)", seen[0])
        row = self.summary_row((snaps / "3.md").read_text(), "a")
        self.assertIn("| running | 0 | 3 | claude -> codex |", row)
        self.assertIn("attempt 3 on codex", row)
        done = self.summary_row(summary.read_text(), "a")
        self.assertIn("| done | 1 | 3 |", done)
        self.assertNotIn("attempt", done)

    def test_summary_shows_transient_retry(self):
        self.add_task("a")
        summary = self.run_dir / "results" / "SUMMARY.md"
        seen = []

        class SnapClock(FakeClock):
            def sleep_until(clock, when, should_stop=lambda: False):
                seen.append(summary.read_text())
                return super().sleep_until(when, should_stop)

        transient = {"exit": 1, "stdout": [{"type": "result", "is_error": True,
                                            "result": "API Error: 529 Overloaded"}]}
        self.scenario_is(claude=[transient, {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(clock=SnapClock()), 0)
        self.assertIn("temporary error on claude, retrying in", self.summary_row(seen[0], "a"))

    def test_both_limited_earliest_is_claude_switches_back(self):
        self.add_task("a")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"}],
                         codex=[codex_limit("1:30 AM")])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex", "claude"])

    def test_emergency_brake(self):
        # three different tasks ending fast for no known reason in a row: a shared cause, the run stops
        self.write_config(quick_exit_seconds=120, max_quick_unknown=3)
        for tid in ("a", "b", "c", "d"):
            self.add_task(tid)
        self.scenario_is(claude=[{"exit": 1, "stderr": "mystery"}] * 9)
        self.assertEqual(self.run_baton(), 3)
        self.assertEqual(len(self.calls()), 7)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertEqual(self.state.tasks["c"].status, "stopped")
        self.assertEqual(self.state.tasks["d"].status, "pending")
        self.assertIn("emergency brake", self.log)
        fixtures = list((self.run_dir / "state" / "fixtures").glob("*.err"))
        self.assertEqual(len(fixtures), 7)

    def test_partial_without_commits_is_failed_and_blocks_dependants(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[{"status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertIn("partial with no commits", self.state.tasks["a"].note)
        self.assertEqual(self.state.tasks["b"].status, "skipped")

    def test_no_result_retries_once_then_done(self):
        self.add_task("a")
        self.scenario_is(claude=[{"exit": 0, "dirty": "half.txt"}, {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        second = self.calls()[1]["prompt"]
        self.assertIn("ended without a result file", second)

    def test_transient_waits_and_retries_same_provider(self):
        self.add_task("a")
        self.scenario_is(claude=[{"exit": 1, "stderr": "API Error: 529 overloaded"},
                                 {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude"])
        self.assertIn("retrying in 5s", self.log)

    def test_auth_error_disables_provider(self):
        self.add_task("a")
        self.scenario_is(claude=[{"exit": 1, "stdout": [{"type": "result", "is_error": True,
                                                         "result": "Invalid API key · Please run /login"}]}],
                         codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        self.assertIn("authentication error", self.log)

    def test_restart_resumes_running_task_and_keeps_limit(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.state_dir.mkdir(parents=True)
        # a previous run died mid-task, with claude limited until far ahead and codex disabled
        until = (T0 + timedelta(hours=3)).isoformat()
        (cfg.state_dir / "run.json").write_text(json.dumps({
            "current": "claude", "quick_fails": 0,
            "providers": {"claude": {"limited_until": until, "window": "five_hour", "disabled": None}},
            "tasks": {"a": {"status": "running", "attempts": 2, "start_head": git(self.repo, "rev-parse", "HEAD")}}}))
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        (self.repo / "left.txt").write_text("unsaved\n")
        cfg.providers["codex"].enabled = False
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])          # dirty repo is fine: the task is resumable
        clock = FakeClock()
        rc = Runner(cfg, clock=clock, echo=False).run(tasks)
        self.assertEqual(rc, 0)
        st = load_state(cfg.state_dir / "run.json")
        self.assertEqual(st.tasks["a"].status, "done")
        self.assertEqual(st.tasks["a"].attempts, 3)
        self.assertIn("interrupted run", git(self.repo, "log", "--format=%s"))
        self.assertTrue(any(isinstance(w, datetime) for w in clock.slept))   # waited for claude
        prompt = self.calls()[0]["prompt"]
        self.assertIn("the runner itself was interrupted", prompt)

    def test_finished_tasks_are_skipped_on_rerun(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.calls()), 1)

    def test_agent_switching_branch_stops_run(self):
        self.add_task("a")
        self.add_task("b")
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\n+++\nBuild a.\n')
        self.scenario_is(claude=[{"exit": 0, "status": "done"}])
        # simulate the agent checking out another branch: a fake step cannot, so do it via wrapper
        wrapper = self.root / "bin" / "claude"
        # not for the preflight `--help` probe, which runs in the caller's cwd
        wrapper.write_text(f"#!/bin/sh\ncase \"$*\" in *--help*) ;; *) git checkout -q -b rogue ;; esac\n"
                           f"exec {sys.executable} {HERE / 'fake_agent.py'} claude \"$@\"\n")
        self.assertEqual(self.run_baton(), 3)
        self.assertIn("left repo on 'rogue'", self.log)
        self.assertEqual(self.state.tasks["b"].status, "pending")

    def test_task_base_creates_branch_from_that_base(self):
        git(self.repo, "checkout", "-q", "-b", "dev")
        (self.repo / "dev-only.txt").write_text("dev\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dev only")
        git(self.repo, "checkout", "-q", "develop")
        self.add_task("a", extra='base = "dev"\n')
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertIn("(feat/x from dev)", plan_text(cfg, tasks))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "rev-list", "--count", "dev..feat/x"), "1")
        self.assertIn("dev only", git(self.repo, "log", "--format=%s", "feat/x"))

    def test_task_without_base_uses_default_base(self):
        git(self.repo, "checkout", "-q", "-b", "dev")
        (self.repo / "dev-only.txt").write_text("dev\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dev only")
        git(self.repo, "checkout", "-q", "develop")
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        self.assertIn("(feat/x from develop)", plan_text(cfg, preflight(cfg)[2]))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertNotIn("dev only", git(self.repo, "log", "--format=%s", "feat/x"))

    def test_preflight_rejects_missing_task_base(self):
        self.add_task("a", extra='base = "nope"\n')
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("base nope does not exist" in e for e in errors))

    def test_plan_marks_verifier_and_hides_its_body(self):
        self.add_task("a")
        self.add_verifier("va")
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])      # verify/va starts from feat/x, which the run creates
        text = plan_text(cfg, tasks)
        self.assertIn("va  [verify]", text)
        self.assertIn("(verify/va from feat/x)", text)
        self.assertNotIn("SECRET CANARY", text)

    def test_preflight_rejects_add_dir_inside_verifiers(self):
        self.add_task("a")
        self.add_verifier("va")
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.providers["claude"].add_dirs = [str(self.run_dir / "verifiers")]
        errors, _, _ = preflight(cfg)
        self.assertTrue(any("exposes the verifiers folder" in e for e in errors))

    def settings_of(self, call):
        argv = call["argv"]
        return json.loads(Path(argv[argv.index("--settings") + 1]).read_text())

    def test_implementer_cannot_see_verifiers(self):
        (self.run_dir / "claude-settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(git push:*)"]}}))
        self.write_config()
        cfg_text = (self.run_dir / "baton.toml").read_text().replace(
            "[providers.claude]\n", '[providers.claude]\nsettings = "claude-settings.json"\nadd_dirs = [".."]\n')
        (self.run_dir / "baton.toml").write_text(cfg_text)
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "tests/test_a.py", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        impl = self.calls()[0]
        vdir = str((self.run_dir / "verifiers").resolve())
        deny = self.settings_of(impl)["permissions"]["deny"]
        self.assertIn("Bash(git push:*)", deny)                 # the user's rules are kept
        self.assertIn(f"Read(/{vdir}/**)", deny)
        self.assertIn(f"Bash(*{vdir}*)", deny)
        self.assertIn(f"Read(/{(self.run_dir / 'results' / 'va').resolve()}/**)", deny)
        self.assertIn(f"Read(/{(self.run_dir / 'results' / 'va.md').resolve()})", deny)
        self.assertNotIn(vdir, impl["prompt"])
        self.assertNotIn("SECRET CANARY", impl["prompt"])
        self.assertEqual(self.state.tasks["va"].status, "done")
        self.assertNotIn(vdir, " ".join(impl["argv"]))
        self.assertIn("--add-dir", impl["argv"])                # add_dirs = [".."] is still passed

    def test_verifier_runs_on_own_branch_without_implementer_output(self):
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        impl, ver = self.calls()
        self.assertIn("COMMON RULES", ver["prompt"])
        self.assertIn("SECRET CANARY", ver["prompt"])
        self.assertNotIn("Build a.", ver["prompt"])
        self.assertIn("test files only", ver["prompt"])
        self.assertIn("never commit canary or mutation descriptions", ver["prompt"])
        self.assertIn("name each test by the behavior it checks", ver["prompt"])
        deny = self.settings_of(ver)["permissions"]["deny"]
        results = (self.run_dir / "results").resolve()
        self.assertIn(f"Read(/{results}/a.md)", deny)
        self.assertIn(f"Read(/{results}/a/**)", deny)
        self.assertNotIn(f"Read(/{results}/**)", deny)       # its own result file stays writable
        self.assertFalse(any(f"{results}/va" in r for r in deny), deny)
        self.assertIn(f"Read(/{(self.run_dir / 'tasks').resolve()}/**)", deny)
        self.assertEqual(self.state.tasks["va"].status, "done")
        self.assertEqual(git(self.repo, "log", "--format=%s", "-1", "verify/va"), "claude: test_a.py")
        self.assertIn("claude: a.txt", git(self.repo, "log", "--format=%s", "verify/va"))
        self.assertNotIn("test_a.py", git(self.repo, "log", "--format=%s", "feat/x"))

    def test_codex_implementer_prompt_has_no_verifier_path(self):
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[claude_limit(), {"commit": "test_a.py", "status": "done"}],
                         codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        codex_impl = [c for c in self.calls() if c["provider"] == "codex"][0]
        vdir = str((self.run_dir / "verifiers").resolve())
        self.assertNotIn(vdir, codex_impl["prompt"])
        self.assertNotIn(vdir, " ".join(codex_impl["argv"]))
        self.assertNotIn("SECRET CANARY", codex_impl["prompt"])

    def test_verifier_touching_non_test_files_is_blocked(self):
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "src/__tests__/a.test.ts", "dirty": "app.py", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        va = self.state.tasks["va"]
        self.assertEqual(va.status, "blocked")
        self.assertIn("verifier touched non-test files: app.py", va.note)
        self.assertNotIn("a.test.ts", va.note)

    def test_verifier_test_paths_are_configurable(self):
        self.write_config(verifier_test_paths=["spec/**"])
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "tests/test_a.py", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["va"].status, "blocked")

    def test_failed_verifier_gates_dependents_transitively(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.add_task("c", deps=["b"])
        self.add_task("d")
        # order: a, d (file name), then va
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "d.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual(st["va"].status, "partial")          # partial is not enough for a gate
        self.assertEqual((st["b"].status, st["b"].gated_by), ("gated", "va"))
        self.assertEqual((st["c"].status, st["c"].gated_by), ("gated", "va"))
        self.assertIn("gated by verifier va (partial)", st["c"].note)
        self.assertEqual(st["d"].status, "done")
        self.assertEqual(len(self.calls()), 3)                # b and c never ran
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("| va (verify) |", summary)
        self.assertIn("| b | repo | gated |", summary)

    def test_done_verifier_lets_dependents_run(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_verifier_waits_for_its_provider_instead_of_falling_back(self):
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "test_a.py", "status": "done"}],
                         codex=[{"commit": "a.txt", "status": "done"}, {"status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        self.assertEqual(self.state.tasks["va"].providers, ["claude"])
        self.assertEqual(self.state.tasks["va"].status, "done")
        self.assertEqual(len([c for c in self.calls() if c["provider"] == "codex"]), 1)
        self.assertTrue(any(isinstance(w, datetime) for w in self.clock.slept))
        self.assertIn("task va: allowed providers (claude) limited; codex not allowed for this task",
                      self.log)

    def test_verifier_is_blocked_when_its_provider_cannot_run(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        auth = {"exit": 1, "stdout": [{"type": "result", "is_error": True,
                                       "result": "Invalid API key · Please run /login"}]}
        self.scenario_is(claude=[auth], codex=[{"commit": "a.txt", "status": "done"}, {"status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        va = self.state.tasks["va"]
        self.assertEqual(va.status, "blocked")
        self.assertIn("no verifier provider can run", va.note)
        self.assertEqual(self.state.tasks["b"].status, "gated")
        self.assertEqual(len([c for c in self.calls() if c["provider"] == "codex"]), 1)

    def test_verifier_providers_must_include_an_enabled_provider(self):
        self.add_task("a")
        self.add_verifier("va")
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.providers["claude"].enabled = False
        cfg.start_provider = "codex"
        errors, _, _ = preflight(cfg)
        self.assertTrue(any("verifier_providers" in e for e in errors))

    def test_only_refuses_a_task_gated_by_an_unfinished_verifier(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.add_task("c", deps=["b"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "blocked"},
                                 {"commit": "c.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["c"].status, "gated")
        self.assertEqual(self.run_baton(only="c"), 5)           # transitively gated by va
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.state.tasks["c"].status, "gated")
        self.assertEqual(self.run_baton(only="c", force=True), 0)
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.state.tasks["c"].status, "done")
        self.assertIn("--force past gate va", self.log)

    def test_only_runs_when_verifier_is_done(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "done"},
                                 {"commit": "b.txt", "status": "done"},
                                 {"commit": "b2.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.run_baton(only="b"), 0)
        self.assertEqual(len(self.calls()), 4)

    def test_task_may_start_from_a_branch_its_dependency_creates(self):
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/a"\n+++\nBuild a.\n')
        (self.run_dir / "tasks" / "b.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/b"\nbase = "feat/a"\n'
            'depends_on = ["a"]\n+++\nBuild b.\n')
        (self.run_dir / "tasks" / "c.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/c"\nbase = "feat/a"\n+++\nBuild c.\n')
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertEqual(len(errors), 1)                                           # c does not depend on a
        self.assertTrue(errors[0].startswith("task c: base feat/a does not exist in repo"), errors)
        self.assertIn("baton never fetches", errors[0])
        (self.run_dir / "tasks" / "c.md").unlink()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "log", "--format=%s", "feat/b").splitlines()[:2],
                         ["claude: b.txt", "claude: a.txt"])

    def test_verifier_keeps_add_dirs_to_write_its_result(self):
        cfg_text = (self.run_dir / "baton.toml").read_text().replace(
            "[providers.claude]\n", '[providers.claude]\nadd_dirs = ["results"]\n')
        (self.run_dir / "baton.toml").write_text(cfg_text)
        (self.run_dir / "results").mkdir()
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "test_a.py", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        impl, ver = self.calls()
        self.assertIn("--add-dir", impl["argv"])
        self.assertIn("--add-dir", ver["argv"])

    def test_task_providers_never_fall_back(self):
        self.add_task("a")
        self.add_task("b", extra='providers = ["claude"]\n')
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "b.txt", "status": "done"}],
                         codex=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        text = plan_text(cfg, preflight(cfg)[2])
        b_line = [line for line in text.splitlines() if " b  " in line][0]
        self.assertIn("claude: sonnet medium", b_line)
        self.assertNotIn("codex", b_line)
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude", "codex"])
        self.assertEqual(self.state.tasks["b"].providers, ["claude"])        # waited for claude
        self.assertTrue(any(isinstance(w, datetime) for w in self.clock.slept))
        self.assertIn("task b: allowed providers (claude) limited; codex not allowed for this task, "
                      "sleeping until", self.log)
        self.assertNotIn("all providers limited", self.log)

    def test_task_listing_every_provider_waits_as_all_limited(self):
        self.add_task("a", extra='providers = ["codex", "claude"]\n')
        self.scenario_is(claude=[claude_limit("11:59pm (Asia/Tehran)")],
                         codex=[codex_limit("10:45 PM"), {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn("task a: all providers limited, sleeping until", self.log)
        self.assertNotIn("not allowed", self.log)

    def test_task_providers_blocked_when_none_can_run(self):
        self.add_task("a")
        self.add_task("b", deps=["a"], extra='providers = ["claude"]\n')
        auth = {"exit": 1, "stdout": [{"type": "result", "is_error": True,
                                       "result": "Invalid API key · Please run /login"}]}
        self.scenario_is(claude=[auth], codex=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        b = self.state.tasks["b"]
        self.assertEqual(b.status, "blocked")
        self.assertIn("no allowed provider can run", b.note)
        self.assertEqual(len([c for c in self.calls() if c["provider"] == "codex"]), 1)

    def test_task_providers_need_an_enabled_provider(self):
        self.add_task("a", extra='providers = ["codex"]\n')
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.providers["codex"].enabled = False
        errors, _, _ = preflight(cfg)
        self.assertIn("task a: none of its providers (codex) is enabled", errors)

    def prompts(self):
        return [(c["provider"], c["prompt"].split("Build ")[1].split(".")[0]
                 if "Build " in c["prompt"] else "verify") for c in self.calls()]

    def test_skip_ahead_runs_a_codex_ready_task_while_a_claude_only_task_waits(self):
        self.add_task("a", extra='providers = ["claude"]\npriority = 5\n')
        self.add_task("b")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"}],
                         codex=[{"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.prompts(), [("claude", "a"), ("codex", "b"), ("claude", "a")])
        a = self.state.tasks["a"]
        self.assertEqual((a.status, a.attempts, a.providers), ("done", 2, ["claude"]))
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertIn("task a: claude limited until", self.log)
        self.assertIn("running b meanwhile", self.log)
        self.assertEqual(len(self.clock.slept), 1)      # slept only once nothing else could run
        self.assertIn("claude: a.txt", git(self.repo, "log", "--format=%s", "feat/x"))

    def test_skip_ahead_never_runs_a_dependent_of_the_waiting_task(self):
        self.add_task("a", extra='providers = ["claude"]\n')
        self.add_task("b", deps=["a"])
        self.add_task("c")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}],
                         codex=[{"commit": "c.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.prompts(), [("claude", "a"), ("codex", "c"), ("claude", "a"), ("claude", "b")])
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()},
                         {"a": "done", "b": "done", "c": "done"})

    def test_skip_ahead_sleeps_when_no_other_task_is_ready(self):
        self.add_task("a", extra='providers = ["claude"]\n')
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.prompts(), [("claude", "a"), ("claude", "a"), ("claude", "b")])
        self.assertNotIn("meanwhile", self.log)

    def test_skip_ahead_keeps_the_verifier_gate(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.add_task("z")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, claude_limit("10:40pm (Asia/Tehran)"),
                                 {"commit": "test_a.py", "status": "blocked"}],
                         codex=[{"commit": "z.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.prompts(), [("claude", "a"), ("claude", "verify"), ("codex", "z"),
                                          ("claude", "verify")])
        self.assertIn("running z meanwhile", self.log)
        self.assertEqual(self.state.tasks["va"].status, "blocked")
        self.assertEqual(self.state.tasks["b"].status, "gated")
        self.assertEqual(self.state.tasks["z"].status, "done")

    def test_waiting_task_resumes_after_a_stop(self):
        self.add_task("a", extra='providers = ["claude"]\n')
        self.add_task("b")
        self.scenario_is(claude=[claude_limit("10:40pm (Asia/Tehran)"), {"commit": "a.txt", "status": "done"}],
                         codex=[{"exit": 1, "stderr": "boom"}])
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.max_quick_unknown = 1
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.cfg.quick_exit_s = 3600                       # b's codex failure pulls the emergency brake
        self.assertEqual(r.run(preflight(cfg)[2]), 3)
        st = load_state(cfg.state_dir / "run.json")
        self.assertEqual(st.tasks["a"].status, "waiting")
        start = st.tasks["a"].start_head
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["a"].start_head, start)
        self.assertEqual(self.state.tasks["a"].attempts, 2)

    def test_preflight_rejects_dirty_repo(self):
        self.add_task("a")
        (self.repo / "junk.txt").write_text("x")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("uncommitted changes" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
