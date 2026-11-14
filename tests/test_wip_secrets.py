"""Three owner requests: limits that have passed are dropped when the state is loaded, a task can opt out of
`return_mid_task`, and the WIP secret filter has two configurable tiers (refuse and warn)."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from baton import gitutil
from baton.config import ConfigError, load_config
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.tasks import TaskError, load_tasks
from tests.test_branches import write_task
from tests.test_runner import T0, FakeClock, RunnerFixture, git
from tests.test_vcs_layer import commit_file, make_repo


class ExpiredLimitsTest(RunnerFixture):
    def seed(self, claude=None, codex=None):
        state = self.run_dir / "state"
        state.mkdir(exist_ok=True)
        (state / "run.json").write_text(json.dumps({
            "current": "claude", "quick_fails": 0, "tasks": {},
            "providers": {"claude": claude or {}, "codex": codex or {}}}))

    def past(self, hours=1):
        return (T0 - timedelta(hours=hours)).isoformat()

    def future(self, hours=3):
        return (T0 + timedelta(hours=hours)).isoformat()

    def test_a_limit_that_has_passed_is_dropped_and_logged_when_the_run_starts(self):
        self.seed(claude={"limited_until": self.past(), "window": "5h", "disabled": None, "reset_known": False,
                          "model_limits": {"opus": self.past(2)}})
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.calls()[0]["provider"], "claude")
        d = self.state.provider_status()["claude"]
        self.assertIsNone(d.limited_until)
        self.assertEqual(d.model_limits, {})
        self.assertIn("limit expired: claude", self.log)
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertNotIn("limited until", summary)

    def test_a_future_limit_and_a_disabled_flag_are_kept(self):
        self.seed(claude={"limited_until": self.future(), "window": "5h", "disabled": None},
                  codex={"limited_until": self.past(), "window": "5h", "disabled": "auth error"})
        self.add_task("a", extra='providers = ["claude"]\n')
        cfg = load_config(self.run_dir / "baton.toml")
        r = Runner(cfg, clock=FakeClock(), echo=False)
        self.assertIsNotNone(r.sched.status["claude"].limited_until)
        self.assertEqual(r.sched.status["codex"].disabled, "auth error")
        self.assertIsNone(r.sched.status["codex"].limited_until)

    def test_plan_does_not_show_a_limit_from_the_past(self):
        self.seed(claude={"limited_until": self.past(), "window": "5h", "disabled": None,
                          "model_limits": {"opus": self.past()}})
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        text = plan_text(cfg, tasks, now=T0)
        self.assertNotIn("limited until", text)
        self.assertNotIn("claude available now", text)

    def test_plan_still_shows_a_limit_that_is_ahead(self):
        self.seed(claude={"limited_until": self.future(), "window": "5h", "disabled": None})
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        self.assertIn("claude limited until", plan_text(cfg, tasks, now=T0))


class TaskReturnOptOutTest(RunnerFixture):
    def build(self, extra):
        self.write_config(start_provider="codex", return_mid_task=True)
        self.add_task("a", extra=extra)
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.sched.mark_limited("codex", T0 + timedelta(hours=1), "5h", reset_known=True)
        return cfg, tasks, r

    def test_the_run_wide_setting_applies_without_an_override(self):
        _, tasks, r = self.build("")
        self.assertEqual(r.return_watch(tasks[0], "claude", None), "codex")

    def test_false_in_the_front_matter_turns_it_off_for_that_task(self):
        _, tasks, r = self.build("return_mid_task = false\n")
        self.assertIs(tasks[0].return_mid_task, False)
        self.assertIsNone(r.return_watch(tasks[0], "claude", None))

    def test_true_in_the_front_matter_turns_it_on_when_the_run_has_it_off(self):
        self.write_config(start_provider="codex", return_mid_task=False)
        self.add_task("a", extra="return_mid_task = true\n")
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.sched.mark_limited("codex", T0 + timedelta(hours=1), "5h", reset_known=True)
        self.assertEqual(r.return_watch(tasks[0], "claude", None), "codex")

    def test_plan_says_return_is_off_for_that_task(self):
        cfg, tasks, _ = self.build("return_mid_task = false\n")
        self.assertIn("return: off", plan_text(cfg, tasks, now=T0))

    def test_a_quoted_value_is_a_task_error_naming_the_key(self):
        self.write_config()
        self.add_task("a", extra='return_mid_task = "no"\n')
        cfg = load_config(self.run_dir / "baton.toml")
        with self.assertRaises(TaskError) as e:
            load_tasks(cfg)
        self.assertIn("return_mid_task", str(e.exception))


class WipSecretTiersTest(RunnerFixture):
    def test_secrets_py_is_committed_and_reported_loudly(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "secrets.py", "content": "NAME = 'x'\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "show", "feat/x:secrets.py"), "NAME = 'x'")
        ts = self.state.task("a")
        self.assertEqual(ts.suspect_files, ["secrets.py"])
        self.assertIn("committed secret-looking files: secrets.py", ts.note)
        self.assertIn("WARNING committed a file that looks like it may hold a secret: secrets.py", self.log)
        summary = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("## Warnings", summary)
        self.assertLess(summary.index("## Warnings"), summary.index("| task |"))
        self.assertIn("secrets.py", summary.split("## Warnings")[1].split("| task |")[0])

    def test_env_is_still_refused_and_excluded(self):
        write_task(self, "a", branch="feat/x")
        write_task(self, "b", branch="feat/y")
        self.scenario_is(claude=[{"dirty": ".env", "content": "TOKEN=abc\n", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn("not committed because they look like secrets: .env", self.state.task("a").note)
        self.assertEqual(self.state.task("a").suspect_files, [])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_a_task_without_suspect_files_has_no_warnings_block(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertNotIn("## Warnings", (self.run_dir / "results" / "SUMMARY.md").read_text())

    def test_custom_refuse_patterns_replace_the_default_list(self):
        self.write_config(wip_secret_patterns=["secrets.*"])
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "secrets.py", "content": "x = 1\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn("not committed because they look like secrets: secrets.py", self.state.task("a").note)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_custom_warn_patterns_and_an_empty_list_switch_the_tier_off(self):
        self.write_config(wip_warn_patterns=["*.cfg"])
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "app.cfg", "content": "x = 1\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.task("a").suspect_files, ["app.cfg"])

    def test_an_empty_warn_list_reports_nothing(self):
        self.write_config(wip_warn_patterns=[])
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "secrets.py", "content": "x = 1\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.task("a").suspect_files, [])
        self.assertEqual(git(self.repo, "show", "feat/x:secrets.py"), "x = 1")

    def test_the_patterns_are_checked_strictly(self):
        for key, bad in (("wip_secret_patterns", ".env"), ("wip_warn_patterns", [1]),
                         ("wip_secret_patterns", [""]), ("wip_warn_patterns", 5)):
            self.write_config(**{key: bad})
            with self.assertRaises(ConfigError, msg=key) as e:
                load_config(self.run_dir / "baton.toml")
            self.assertIn(key, str(e.exception))

    def test_service_account_json_is_warned_about(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "service-account-prod.json", "content": "{}\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.task("a").suspect_files, ["service-account-prod.json"])


class TierFunctionsTest(unittest.TestCase):
    def setUp(self):
        self.top = make_repo(Path(tempfile.mkdtemp()) / "mono")
        commit_file(self.top, "api/app.py", "print(1)\n", "init")
        self.sub = self.top / "api"

    def test_both_tiers_work_from_a_folder_inside_the_repository(self):
        (self.sub / "secrets.py").write_text("x = 1\n")
        (self.sub / ".env").write_text("TOKEN=abc\n")
        (self.sub / ".env.example").write_text("TOKEN=\n")
        (self.sub / "ok.py").write_text("y = 2\n")
        self.assertEqual(gitutil.secret_looking(self.sub), ["api/.env"])
        self.assertEqual(gitutil.suspect_looking(self.sub), ["api/secrets.py"])

    def test_commit_takes_the_warned_file_and_leaves_the_refused_one(self):
        (self.sub / "secrets.py").write_text("x = 1\n")
        (self.sub / ".env").write_text("TOKEN=abc\n")
        gitutil.commit_all(self.sub, "wip")
        files = git(self.top, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertIn("api/secrets.py", files)
        self.assertNotIn("api/.env", files)

    def test_a_pattern_with_a_slash_matches_the_whole_path(self):
        (self.sub / "conf").mkdir()
        (self.sub / "conf" / "local.ini").write_text("a\n")
        (self.sub / "local.ini").write_text("b\n")
        self.assertEqual(gitutil.secret_looking(self.sub, ["api/conf/*.ini"]), ["api/conf/local.ini"])

    def test_the_refused_tier_wins_over_the_warn_tier(self):
        (self.sub / "credentials.pem").write_text("x\n")
        self.assertEqual(gitutil.secret_looking(self.sub), ["api/credentials.pem"])
        self.assertEqual(gitutil.suspect_looking(self.sub), [])

    def test_matching_ignores_case(self):
        self.assertTrue(gitutil.looks_secret("Keys/ID_RSA"))
        self.assertTrue(gitutil.looks_suspect("Config/Secrets.PY"))
        self.assertFalse(gitutil.looks_secret(".env.example"))


if __name__ == "__main__":
    unittest.main()
