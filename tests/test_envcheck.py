"""env-preflight: the read-only environment verifier, and its automatic scheduling."""

import unittest

from baton.checks import CheckContext, run_template
from baton.config import load_config
from baton.preflight import plan_text, preflight
from baton.tasks import load_tasks
from tests.test_runner import RunnerFixture

SECRET = "s3cr3t-value-do-not-print"


class EnvPreflightTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.api = self.root / "api"
        self.panel = self.root / "panel"
        self.api.mkdir()
        self.panel.mkdir()
        (self.api / ".env.example").write_text(
            "MONGO_URL=\nJWT_SECRET=\nCORS_ORIGINS=\nALLOWED_REDIRECT_URLS=\n"
            "INDIVIDUAL_PANEL_BASE_URL=\n# COMMENTED=1\nSENTRY_DSN=  # optional\n")
        self.write_api_env()
        (self.panel / ".env.example").write_text("NEXT_PUBLIC_API_BASE_URL=\n")
        (self.panel / ".env.local").write_text("NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000\n")
        self.configure()

    def write_api_env(self, cors="http://127.0.0.1:3001,http://127.0.0.1:3002",
                      redirects='["http://127.0.0.1:3001/auth/done", "http://127.0.0.1:3002/auth/done"]',
                      extra=""):
        (self.api / ".env").write_text(
            f"MONGO_URL=mongodb://u:{SECRET}@localhost/db\nJWT_SECRET={SECRET}\n"
            f"CORS_ORIGINS={cors}\nALLOWED_REDIRECT_URLS={redirects}\n"
            f"INDIVIDUAL_PANEL_BASE_URL=http://127.0.0.1:3002\n{extra}")

    def configure(self, callback="http://127.0.0.1:8000/auth/callback", extra=""):
        self.write_config(extra=f"""
[preflight]
repos = ["../api", "../panel"]
backend = "../api"
panel_origins = ["http://127.0.0.1:3001", "http://127.0.0.1:3002"]
auth_callback = "{callback}"
{extra}""")

    def check(self):
        cfg = load_config(self.run_dir / "baton.toml")
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        (self.run_dir / "verifiers" / "pre.md").write_text(
            '+++\nkind = "verify"\ntemplate = "env-preflight"\n+++\n')
        task = next(t for t in load_tasks(cfg) if t.id == "pre")
        return run_template(CheckContext(cfg, task, cfg.results_dir / "pre"))

    def test_clean_workspace_passes(self):
        res = self.check()
        self.assertEqual(res.gaps, [])
        self.assertEqual(res.status, "done")
        self.assertTrue(any("api: all 5 keys" in c for c in res.checked), res.checked)

    def test_missing_and_empty_keys_are_named_without_values(self):
        (self.api / ".env").write_text(f"MONGO_URL=mongodb://u:{SECRET}@h/db\nJWT_SECRET=\n"
                                       "CORS_ORIGINS=http://127.0.0.1:3001,http://127.0.0.1:3002\n")
        res = self.check()
        text = "\n".join(res.gaps)
        self.assertIn("api: 2 key(s) of .env.example not set in .env: ALLOWED_REDIRECT_URLS, INDIVIDUAL_PANEL_BASE_URL", text)
        self.assertIn("set but empty in .env: JWT_SECRET", text)
        self.assertNotIn("SENTRY_DSN", text)          # marked optional
        self.assertNotIn(SECRET, text + "\n".join(res.checked))

    def test_a_missing_env_file_is_one_gap(self):
        (self.panel / ".env.local").unlink()
        res = self.check()
        self.assertTrue(any(g.startswith("panel: none of .env, .env.local") for g in res.gaps), res.gaps)

    def test_cors_must_list_every_panel_origin(self):
        self.write_api_env(cors="http://127.0.0.1:3001/")
        res = self.check()
        self.assertEqual([g for g in res.gaps if "CORS" in g],
                         ["api: CORS_ORIGINS (CORS) does not include http://127.0.0.1:3002"])

    def test_redirect_allow_list_must_list_every_panel_origin(self):
        self.write_api_env(redirects="http://127.0.0.1:3001/*")
        res = self.check()
        self.assertEqual([g for g in res.gaps if "redirect" in g],
                         ["api: ALLOWED_REDIRECT_URLS (auth redirect allow-list) does not include http://127.0.0.1:3002"])

    def test_wildcard_cors_passes(self):
        self.write_api_env(cors="*")
        self.assertEqual(self.check().gaps, [])

    def test_no_cors_key_at_all_is_a_gap(self):
        self.write_api_env(cors="")
        res = self.check()
        self.assertTrue(any("none of CORS_ORIGINS" in g for g in res.gaps), res.gaps)

    def test_mixing_localhost_and_127_is_a_gap(self):
        self.write_api_env(cors="http://localhost:3001,http://localhost:3002")
        res = self.check()
        [gap] = [g for g in res.gaps if "loopback" in g]
        self.assertIn("127.0.0.1 in auth_callback, panel_origins", gap)
        self.assertIn("localhost in CORS list", gap)

    def test_callback_on_another_loopback_name_is_a_gap(self):
        self.configure(callback="http://localhost:8000/auth/callback")
        res = self.check()
        self.assertTrue(any("loopback hosts are mixed" in g and "localhost in auth_callback" in g
                            for g in res.gaps), res.gaps)

    def test_a_missing_backend_is_reported_not_skipped(self):
        self.write_config(extra='[preflight]\nrepos = ["../panel"]\npanel_origins = ["http://127.0.0.1:3001"]')
        res = self.check()
        self.assertTrue(any("[preflight] backend is not set" in g for g in res.gaps), res.gaps)


class AutoScheduleTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.api = self.root / "api"
        self.api.mkdir()
        (self.api / ".env.example").write_text("MONGO_URL=\n")
        (self.api / ".env").write_text("MONGO_URL=mongodb://localhost/db\n")
        self.pre = '[preflight]\nrepos = ["../api"]\n'

    def ids(self, tasks):
        return [t.id for t in tasks]

    def plan(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        return cfg, tasks

    def test_scheduled_after_the_last_task_of_a_feature(self):
        self.write_config(extra=self.pre)
        self.add_task("a", extra='feature = "onboarding"\n')
        self.add_task("b", deps=["a"], extra='feature = "onboarding"\n')
        self.add_task("other")
        cfg, tasks = self.plan()
        pre = next(t for t in tasks if t.template == "env-preflight")
        self.assertEqual((pre.id, pre.depends_on, pre.auto), ("env-preflight-onboarding", ["a", "b"], True))
        self.assertLess(self.ids(tasks).index("b"), self.ids(tasks).index(pre.id))
        text = plan_text(cfg, tasks)
        self.assertIn("env-preflight-onboarding  [verify: env-preflight, auto]", text)
        self.assertIn("runs in baton, no agent", text)
        self.assertIn("first command", text)          # the first agent task, not the check

    def test_an_approval_in_the_feature_waits_for_it(self):
        self.write_config(extra=self.pre)
        self.add_task("a", extra='feature = "onboarding"\n')
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\nfeature = "onboarding"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.add_task("after", deps=["gate"], extra='feature = "onboarding"\n')
        _, tasks = self.plan()
        by_id = {t.id: t for t in tasks}
        self.assertEqual(by_id["gate"].depends_on, ["a", "env-preflight-onboarding"])
        self.assertEqual(by_id["env-preflight-onboarding"].depends_on, ["a"])   # not the task after the gate

    def test_not_scheduled_without_config_without_feature_or_when_off(self):
        self.write_config()
        self.add_task("a", extra='feature = "onboarding"\n')
        self.assertEqual(self.ids(self.plan()[1]), ["a"])
        self.write_config(extra=self.pre)
        self.add_task("b")
        self.assertNotIn("env-preflight-None", self.ids(self.plan()[1]))
        self.write_config(extra=self.pre + "auto = false")
        self.assertEqual(self.ids(self.plan()[1]), ["a", "b"])

    def test_a_hand_written_preflight_replaces_the_automatic_one(self):
        self.write_config(extra=self.pre)
        self.add_task("a", extra='feature = "onboarding"\n')
        (self.run_dir / "verifiers").mkdir()
        (self.run_dir / "verifiers" / "mine.md").write_text(
            '+++\nkind = "verify"\ntemplate = "env-preflight"\nfeature = "onboarding"\ndepends_on = ["a"]\n+++\n')
        self.assertEqual(self.ids(self.plan()[1]), ["a", "mine"])

    def test_runs_without_an_agent_and_gates_what_follows(self):
        self.write_config(extra=self.pre)
        self.add_task("a", extra='feature = "onboarding"\n')
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\nfeature = "onboarding"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        (self.api / ".env").write_text("OTHER=1\n")           # MONGO_URL is missing
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual(len(self.calls()), 1)                # only task a used an agent
        self.assertEqual(st["env-preflight-onboarding"].status, "blocked")
        self.assertEqual(st["gate"].status, "gated")
        report = (self.run_dir / "results" / "env-preflight-onboarding.md").read_text()
        self.assertTrue(report.startswith("STATUS: blocked"))
        self.assertIn("MONGO_URL", report)
        self.assertEqual(git_branch(self.repo), "develop")    # the repository is back where it was

    def test_a_clean_environment_lets_the_checkpoint_ask(self):
        self.write_config(extra=self.pre)
        self.add_task("a", extra='feature = "onboarding"\n')
        (self.run_dir / "tasks" / "gate.md").write_text(
            '+++\nkind = "approve"\nfeature = "onboarding"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["env-preflight-onboarding"].status, "done")
        self.assertEqual(self.state.tasks["gate"].status, "awaiting")


def git_branch(repo):
    from tests.test_runner import git
    return git(repo, "branch", "--show-current")


if __name__ == "__main__":
    unittest.main()
