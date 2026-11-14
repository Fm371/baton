"""Strict config and task validation, the timezone warning, a side-effect-free
`baton plan`, the results folder for Claude, and the shipped example files."""

import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

from baton import cli
from baton.config import ConfigError, load_config
from baton.isolation import with_results_dir
from baton.preflight import timezone_warning
from baton.tasks import TaskError, load_tasks
from tests.test_runner import RunnerFixture

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "baton" / "templates" / "task-auto"

BASE = """
[run]
{run}
[providers.claude]
bin = "claude"
{claude}
[routing.rungs.r1]
claude = {{ model = "haiku" }}
[routing.work_types]
docs = "r1"
"""


class ConfigValidationTest(unittest.TestCase):
    def load(self, run="", claude=""):
        d = Path(tempfile.mkdtemp())
        (d / "baton.toml").write_text(BASE.format(run=textwrap.dedent(run), claude=claude))
        return load_config(d / "baton.toml")

    def rejects(self, pattern, **kw):
        with self.assertRaises(ConfigError) as e:
            self.load(**kw)
        self.assertRegex(str(e.exception), pattern)
        self.assertTrue(str(e.exception).startswith("baton.toml"), str(e.exception))   # names the file

    def test_a_good_config_loads_with_the_documented_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.task_timeout_min, 240)
        self.assertEqual(cfg.max_quick_unknown, 3)
        self.assertIsNone(cfg.timezone)

    def test_a_string_where_a_number_belongs_is_a_config_error_naming_the_key(self):
        self.rejects(r"run\.task_timeout_min must be a number, got 'abc'", run='task_timeout_min = "abc"')
        self.rejects(r"run\.max_turns must be a whole number", run="max_turns = 2.5")
        self.rejects(r"run\.max_sleep_hours must be a number", run="max_sleep_hours = true")

    def test_ranges_are_checked(self):
        self.rejects(r"run\.max_quick_unknown must be at least 1", run="max_quick_unknown = 0")
        self.rejects(r"run\.task_timeout_min must be greater than 0", run="task_timeout_min = 0")
        self.rejects(r"run\.reset_margin_seconds must be at least 0", run="reset_margin_seconds = -5")
        self.rejects(r"run\.follow_poll_seconds must be greater than 0", run="follow_poll_seconds = 0")

    def test_a_misspelled_run_key_is_rejected_with_a_suggestion(self):
        self.rejects(r"\[run\]: unknown key 'max_sleep_hour' \(did you mean 'max_sleep_hours'\?\)",
                     run="max_sleep_hour = 1")

    def test_an_unknown_key_without_a_near_match_is_still_rejected(self):
        self.rejects(r"unknown key 'zzzzzz'$", run="zzzzzz = 1")

    def test_a_misspelled_provider_key_is_rejected(self):
        self.rejects(r"\[providers\.claude\]: unknown key 'permision_mode' \(did you mean 'permission_mode'\?\)",
                     claude='permision_mode = "plan"')

    def test_an_unknown_top_level_table_is_rejected(self):
        d = Path(tempfile.mkdtemp())
        (d / "baton.toml").write_text(BASE.format(run="", claude="") + "\n[runn]\nx = 1\n")
        with self.assertRaisesRegex(ConfigError, r"top level: unknown key 'runn' \(did you mean 'run'\?\)"):
            load_config(d / "baton.toml")

    def test_a_bad_timezone_names_the_key(self):
        self.rejects(r"run\.timezone: unknown timezone 'Mars/Base'", run='timezone = "Mars/Base"')

    def test_known_keys_all_still_load(self):
        cfg = self.load(run=textwrap.dedent("""
            start_provider = "claude"
            task_timeout_min = 30.5
            max_turns = 10
            commit_leftovers = false
            lanes = 2
            follow_poll_seconds = 1.5
            """), claude='max_concurrent = 2\nenabled = true')
        self.assertEqual((cfg.task_timeout_min, cfg.max_turns, cfg.lanes), (30.5, 10, 2))


class TaskValidationTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "baton.toml").write_text(BASE.format(run="", claude=""))
        (self.d / "tasks").mkdir()
        (self.d / "repo").mkdir()
        self.cfg = load_config(self.d / "baton.toml")

    def load(self, meta):
        (self.d / "tasks" / "t.md").write_text(f'+++\nrepo = "repo"\nwork_type = "docs"\n{meta}\n+++\nDo it.\n')
        return load_tasks(self.cfg)

    def test_good_values_load(self):
        [t] = self.load("priority = 5\ntimeout_min = 12.5\nmax_turns = 40")
        self.assertEqual((t.priority, t.timeout_min, t.max_turns), (5, 12.5, 40))

    def test_bad_values_are_a_task_error_with_file_and_key(self):
        for meta, pattern in [('priority = "high"', r"t\.md: priority must be a whole number, got 'high'"),
                              ('timeout_min = "soon"', r"t\.md: timeout_min must be a number"),
                              ("timeout_min = 0", r"t\.md: timeout_min must be greater than 0"),
                              ("max_turns = 1.5", r"t\.md: max_turns must be a whole number"),
                              ("max_turns = -1", r"t\.md: max_turns must be at least 0")]:
            with self.subTest(meta=meta), self.assertRaisesRegex(TaskError, pattern):
                self.load(meta)


class TimezoneWarningTest(RunnerFixture):
    def test_a_zone_that_is_not_the_machines_warns(self):
        cfg = load_config(self.run_dir / "baton.toml")          # Asia/Tehran
        machine = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        local = machine.astimezone().utcoffset()
        text = timezone_warning(cfg, machine)
        if local == cfg.timezone.utcoffset(machine.replace(tzinfo=None)):
            self.assertIsNone(text)                              # the test machine happens to be in Tehran
        else:
            self.assertIn("run.timezone = Asia/Tehran", text)
            self.assertIn("remove run.timezone", text)

    def test_the_machines_own_zone_and_no_zone_do_not_warn(self):
        self.write_config(timezone="")
        self.assertIsNone(timezone_warning(load_config(self.run_dir / "baton.toml")))
        here = datetime.now(timezone.utc).astimezone().tzinfo
        name = getattr(here, "key", None)
        if name:                                                 # an IANA-named local zone
            self.write_config(timezone=name)
            self.assertIsNone(timezone_warning(load_config(self.run_dir / "baton.toml")))


class PlanTest(RunnerFixture):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([args[0], "-c", str(self.run_dir / "baton.toml"), *args[1:]])
        return rc, out.getvalue(), err.getvalue()

    def test_plan_creates_no_results_state_or_logs_folder(self):
        self.add_task("a")
        rc, out, _ = self.run_cli("plan")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing was run and nothing was created", out)
        for name in ("results", "state", "logs"):
            self.assertFalse((self.run_dir / name).exists(), name)

    def test_run_still_creates_them(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        for name in ("results", "state", "logs"):
            self.assertTrue((self.run_dir / name).is_dir(), name)

    def test_plan_reports_a_folder_it_could_not_write(self):
        self.add_task("a")
        (self.run_dir / "results").write_text("a file, not a folder")
        rc, out, _ = self.run_cli("plan")
        self.assertEqual(rc, 1)
        self.assertIn("cannot write to", out)

    def test_the_first_command_is_shell_quoted(self):
        self.add_task("a")
        text = (self.run_dir / "baton.toml").read_text()
        (self.run_dir / "baton.toml").write_text(
            text.replace("[providers.claude]", '[providers.claude]\nextra_args = ["--append-system-prompt", "two words"]', 1))
        _, out, _ = self.run_cli("plan")
        self.assertIn("'two words'", out)

    def test_a_repo_that_is_not_a_git_repository_says_where_the_path_is_relative_to(self):
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../nowhere"\nwork_type = "feature"\n+++\nBuild a.\n')
        (self.root / "nowhere").mkdir()
        rc, out, _ = self.run_cli("plan")
        self.assertEqual(rc, 1)
        self.assertIn("relative to baton.toml's folder", out)


class ClassifyCommandTest(RunnerFixture):
    def test_an_unknown_timezone_is_a_clean_error(self):
        sample = self.root / "out.txt"
        sample.write_text("")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["classify", "claude", str(sample), "--timezone", "Mars/Base"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown timezone 'Mars/Base'", err.getvalue())

    def test_a_bad_now_is_a_clean_error(self):
        sample = self.root / "out.txt"
        sample.write_text("")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["classify", "claude", str(sample), "--now", "yesterday"])
        self.assertEqual(rc, 2)
        self.assertIn("--now", err.getvalue())


class ResultsDirTest(unittest.TestCase):
    def test_it_is_added_when_nothing_covers_it(self):
        d = Path(tempfile.mkdtemp())
        (d / "results").mkdir()
        (d / "repo").mkdir()
        self.assertEqual(with_results_dir(["/x"], d / "results", d / "repo"), ["/x", str((d / "results").resolve())])

    def test_it_is_not_added_twice_or_when_a_parent_or_the_repo_covers_it(self):
        d = Path(tempfile.mkdtemp())
        (d / "results").mkdir()
        (d / "repo").mkdir()
        res = str((d / "results").resolve())
        self.assertEqual(with_results_dir([res], d / "results", d / "repo"), [res])
        self.assertEqual(with_results_dir([str(d)], d / "results", d / "repo"), [str(d)])
        self.assertEqual(with_results_dir([], d / "results", d), [])             # results inside the repo
        inside = d / "repo" / "results"
        inside.mkdir()
        self.assertEqual(with_results_dir([], inside, d / "repo"), [])


class ResultsDirReachesClaudeTest(RunnerFixture):
    def test_the_claude_command_carries_the_results_folder(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        [call] = self.calls()
        argv = call["argv"]
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str((self.run_dir / "results").resolve()))


class ShippedExamplesTest(unittest.TestCase):
    def test_the_example_config_has_no_forced_timezone_and_the_code_default_timeout(self):
        cfg = load_config(EXAMPLE / "baton.toml")
        self.assertIsNone(cfg.timezone)
        self.assertEqual(cfg.task_timeout_min, 240)

    def test_the_example_settings_cannot_be_walked_around(self):
        perms = json.loads((EXAMPLE / "claude-settings.example.json").read_text())["permissions"]
        allow, deny = perms["allow"], perms["deny"]
        # prefixes that let a command run anything or read anything
        for bad in ("python3", "python", "node", "npx", "find", "cat", "bash", "sh", "env", "xargs", "curl"):
            self.assertFalse(any(a.startswith(f"Bash({bad}:") or a == f"Bash({bad})" for a in allow), bad)
        for needed in ("Bash(git push:*)", "Bash(git reset:*)", "Bash(git clean:*)", "Read(**/.env)",
                       "Read(~/.ssh/**)", "Edit(**/.env)", "Bash(rm -rf:*)", "Bash(sudo:*)"):
            self.assertIn(needed, deny)
        # a deny rule beats an allow rule, so an allowed command that is also denied never works
        self.assertEqual([a for a in allow if a in deny], [])
        for runner in ("Bash(pytest:*)", "Bash(npm test:*)", "Bash(go test:*)", "Bash(cargo test:*)",
                       "Bash(make test:*)"):
            self.assertIn(runner, allow)


if __name__ == "__main__":
    unittest.main()
