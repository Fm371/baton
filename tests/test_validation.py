"""Strict booleans and types, clean errors instead of tracebacks, branch names, warnings for
switched-off guardrails and for lanes that cannot overlap, and the order of the run lock."""

import contextlib
import fcntl
import io
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from baton import cli
from baton.config import ConfigError, load_config
from baton.preflight import preflight
from baton.tasks import TaskError, load_tasks
from baton.watch import run_active
from tests.test_runner import RunnerFixture

BASE = """
[run]
{run}
[providers.claude]
{claude}
[routing.rungs.r1]
claude = {{ model = "haiku" }}
[routing.work_types]
docs = "r1"
{rest}
"""


class StrictTypesTest(unittest.TestCase):
    def load(self, run="", claude="", rest=""):
        d = Path(tempfile.mkdtemp())
        (d / "baton.toml").write_text(BASE.format(run=textwrap.dedent(run), claude=claude, rest=rest))
        return load_config(d / "baton.toml")

    def rejects(self, pattern, **kw):
        with self.assertRaises(ConfigError) as e:
            self.load(**kw)
        self.assertRegex(str(e.exception), pattern)

    def test_a_quoted_false_is_not_false(self):
        for key in ("commit_leftovers", "hardcoded_key_check", "return_to_start_provider"):
            self.rejects(rf"run\.{key} must be true or false \(without quotes\), not 'false'",
                         run=f'{key} = "false"')

    def test_provider_booleans_are_strict(self):
        self.rejects(r"providers\.claude\.enabled must be true or false", claude='enabled = "false"')
        self.rejects(r"providers\.claude\.effort_flag must be true or false", claude="effort_flag = 0")

    def test_preflight_auto_is_strict(self):
        self.rejects(r"preflight\.auto must be true or false", rest='[preflight]\nauto = "no"')

    def test_real_booleans_still_work(self):
        cfg = self.load(run="commit_leftovers = false\nhardcoded_key_check = false",
                        claude="enabled = true\neffort_flag = false")
        self.assertFalse(cfg.commit_leftovers)
        self.assertFalse(cfg.hardcoded_key_check)
        self.assertFalse(cfg.providers["claude"].effort_flag)

    def test_wrong_types_are_config_errors_not_tracebacks(self):
        self.rejects(r"run\.default_base must be a string", run="default_base = 5")
        self.rejects(r"run\.start_provider must be a string", run="start_provider = [1]")
        self.rejects(r"providers\.claude\.permission_mode must be a string", claude="permission_mode = 3")
        self.rejects(r"providers\.claude\.bin must be a string", claude="bin = 3")

    def test_a_rung_that_is_not_a_table_is_explained(self):
        self.rejects(r"routing\.rungs\.r2 must be a table with one sub-table per provider",
                     rest='[routing.rungs]\nr2 = "x"')

    def test_routing_tables_of_the_wrong_kind(self):
        d = Path(tempfile.mkdtemp())
        (d / "baton.toml").write_text('[providers.claude]\nbin = "claude"\n[routing]\nrungs = 5\n')
        with self.assertRaises(ConfigError) as e:
            load_config(d / "baton.toml")
        self.assertIn("[routing.rungs] must be a table", str(e.exception))


class TaskFrontMatterTest(RunnerFixture):
    def load(self, meta):
        wt = "" if meta.startswith("work_type") else 'work_type = "feature"\n'
        (self.run_dir / "tasks" / "a.md").write_text(f'+++\nrepo = "../repo"\n{wt}{meta}\n+++\nBuild a.\n')
        return load_tasks(load_config(self.run_dir / "baton.toml"))

    def test_wrong_types_in_front_matter_are_task_errors(self):
        for meta, word in (("depends_on = 5", "depends_on"), ("branch = 5", "branch"),
                           ('base = ["a"]', "base"), ("work_type = 5", "work_type"),
                           ("feature = 3", "feature"), ("depends_on = [1]", "depends_on")):
            with self.subTest(meta=meta):
                with self.assertRaises(TaskError) as e:
                    self.load(meta)
                self.assertIn(word, str(e.exception))
                self.assertIn("a.md", str(e.exception))

    def test_a_string_dependency_still_works(self):
        self.assertEqual(self.load('depends_on = "b"')[0].depends_on, ["b"])


class BranchNameTest(RunnerFixture):
    def plan(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, _ = preflight(cfg, create_dirs=False)
        return errors

    def test_an_invalid_branch_in_the_file_fails_the_plan(self):
        self.add_task("a", extra='branch = "bad..name"\n')
        text = (self.run_dir / "tasks" / "a.md").read_text().replace('branch = "feat/x"\n', "")
        (self.run_dir / "tasks" / "a.md").write_text(text)
        errors = self.plan()
        self.assertTrue(any("'bad..name' is not a valid branch name" in e for e in errors), errors)

    def test_a_task_id_that_cannot_be_part_of_a_branch_says_to_rename_the_file(self):
        for tid in ("v1..x", "x.lock"):
            with self.subTest(tid=tid):
                (self.run_dir / "tasks" / f"{tid}.md").write_text(
                    '+++\nrepo = "../repo"\nwork_type = "feature"\n+++\nBuild.\n')
                errors = self.plan()
                (self.run_dir / "tasks" / f"{tid}.md").unlink()
                self.assertTrue(any(f"baton/{tid}" in e and "rename the task file" in e for e in errors), errors)

    def test_ordinary_names_pass(self):
        self.add_task("a")
        self.assertEqual(self.plan(), [])


class UnsafeModeTest(RunnerFixture):
    def warnings(self):
        cfg = load_config(self.run_dir / "baton.toml")
        return preflight(cfg, create_dirs=False)[1]

    def edit(self, old, new):
        p = self.run_dir / "baton.toml"
        p.write_text(p.read_text().replace(old, new, 1))

    def test_the_default_mode_is_quiet(self):
        self.add_task("a")
        self.assertFalse([w for w in self.warnings() if "switches the agent" in w])

    def test_bypass_permissions_is_allowed_but_warned_about(self):
        self.add_task("a")
        self.edit("[providers.claude]", '[providers.claude]\npermission_mode = "bypassPermissions"')
        ws = [w for w in self.warnings() if "switches the agent" in w]
        self.assertEqual(len(ws), 1)
        self.assertIn("claude: permission_mode = 'bypassPermissions'", ws[0])

    def test_skip_permission_flags_are_warned_about(self):
        self.add_task("a")
        self.edit("[providers.claude]", '[providers.claude]\nextra_args = ["--dangerously-skip-permissions"]')
        self.edit("[providers.codex]", '[providers.codex]\nextra_args = ["--dangerously-bypass-approvals-and-sandbox"]')
        ws = [w for w in self.warnings() if "switches the agent" in w]
        self.assertEqual(len(ws), 2, ws)


class LanesOverlapWarningTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.other = self.root / "other"
        self.other.mkdir()
        for args in (("init", "-q", "-b", "develop"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(self.other), *args], check=True, capture_output=True)
        (self.other / "README").write_text("x\n")
        subprocess.run(["git", "-C", str(self.other), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.other), "commit", "-q", "-m", "init"], check=True, capture_output=True)

    def task(self, tid, repo, providers=""):
        (self.run_dir / "tasks" / f"{tid}.md").write_text(
            f'+++\nrepo = "../{repo}"\nwork_type = "feature"\nbranch = "feat/{tid}"\n{providers}+++\nBuild.\n')

    def warnings(self, claude_slots=1):
        p = self.run_dir / "baton.toml"
        if claude_slots != 1:
            p.write_text(p.read_text().replace("[providers.claude]", f"[providers.claude]\nmax_concurrent = {claude_slots}", 1))
        cfg = load_config(p)
        return [w for w in preflight(cfg, create_dirs=False)[1] if "nothing can run in parallel" in w]

    def test_two_repos_on_the_same_provider_with_one_slot_cannot_overlap(self):
        self.write_config(lanes=2)
        self.task("a", "repo")
        self.task("b", "other")
        ws = self.warnings()
        self.assertEqual(len(ws), 1)
        self.assertIn("max_concurrent", ws[0])
        self.assertIn('providers = ["codex"]', ws[0])

    def test_a_codex_task_gives_the_second_lane_somewhere_to_go(self):
        self.write_config(lanes=2)
        self.task("a", "repo")
        self.task("b", "other", 'providers = ["codex"]\n')
        self.assertEqual(self.warnings(), [])

    def test_two_claude_slots_allow_overlap(self):
        self.write_config(lanes=2)
        self.task("a", "repo")
        self.task("b", "other")
        self.assertEqual(self.warnings(claude_slots=2), [])

    def test_one_lane_has_its_own_warning_and_lanes_one_has_none(self):
        self.write_config(lanes=1)
        self.task("a", "repo")
        self.task("b", "other")
        self.assertEqual(self.warnings(), [])


class RunLockTest(RunnerFixture):
    def baton(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([args[0], "-c", str(self.run_dir / "baton.toml"), *args[1:]])
        return rc, out.getvalue() + err.getvalue()

    def test_probing_creates_nothing(self):
        cfg = load_config(self.run_dir / "baton.toml")
        self.assertFalse(run_active(cfg))
        self.assertFalse(cfg.state_dir.exists())

    def test_stop_with_no_run_creates_no_state_folder(self):
        rc, out = self.baton("stop")
        self.assertEqual(rc, 1)
        self.assertIn("no baton run is active", out)
        self.assertFalse((self.run_dir / "state").exists())

    def test_a_second_run_is_refused_before_preflight_touches_anything(self):
        self.add_task("a")
        state = self.run_dir / "state"
        state.mkdir()
        with (state / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rc, out = self.baton("run")
        self.assertEqual(rc, 4)
        self.assertIn("already active", out)
        self.assertNotIn("warning", out)                       # preflight did not run
        self.assertFalse((self.run_dir / "results").exists())
        self.assertFalse((self.run_dir / "logs").exists())

    def test_a_probe_does_not_block_a_run(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        state = self.run_dir / "state"
        state.mkdir()
        (state / "lock").touch()
        cfg = load_config(self.run_dir / "baton.toml")
        with (state / "lock").open("rb") as probe:
            fcntl.flock(probe, fcntl.LOCK_SH)                  # what `status` / `watch` hold for a moment
            import threading
            threading.Timer(0.3, lambda: fcntl.flock(probe, fcntl.LOCK_UN)).start()
            self.assertTrue(run_active(cfg) is False)          # a shared lock is no run
            self.assertEqual(self.run_baton(), 0)              # waits out the probe instead of refusing


class ClassifyMissingFileTest(RunnerFixture):
    def test_a_missing_sample_is_a_clean_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["classify", "claude", "/nonexistent/out.txt"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot read /nonexistent/out.txt", err.getvalue())


if __name__ == "__main__":
    unittest.main()
