"""real-data-smoke, and the runner's hardcoded-key check."""

import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from baton.checks import CheckContext, run_template
from baton.config import load_config
from baton.tasks import load_tasks
from tests.test_checks_common import free_port
from tests.test_runner import RunnerFixture

FAKE_API = Path(__file__).parent / "fake_api.py"
REAL_DB = "mongodb://user:hunter2@db.local:27017/app_copy"
STEPS = """
[[params.steps]]
name = "log in"
method = "POST"
path = "/auth/login"
json = { username = "qa", password = "${QA_PASSWORD}" }
capture = { token = "access_token" }
[[params.steps]]
name = "enroll through the invite link"
method = "POST"
path = "/plans/p1/enroll"
headers = { Authorization = "Bearer {token}" }
expect_json = { status = "enrolled" }
"""


class RealDataSmokeTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        patcher = mock.patch.dict(os.environ, {"QA_PASSWORD": "pw-from-env"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configure()

    def configure(self, db_url=REAL_DB, **over):
        server = {"name": "api", "cmd": f"{sys.executable} {FAKE_API} {self.port}",
                  "url": f"http://127.0.0.1:{self.port}/health"}
        opts = {"smoke_db_url": db_url, "db_env": "FAKE_DB", "server": server,
                "base_url": f"http://127.0.0.1:{self.port}", **over}
        opts = {k: v for k, v in opts.items() if v is not None}
        from tests.test_browser_smoke import toml_value
        self.write_config(extra="[real_data_smoke]\n" + "\n".join(
            f"{k} = {toml_value(v)}" for k, v in opts.items()) + "\n")

    def check(self, steps=STEPS):
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        (self.run_dir / "verifiers" / "data.md").write_text(
            '+++\nkind = "verify"\ntemplate = "real-data-smoke"\n' + steps + '+++\n')
        cfg = load_config(self.run_dir / "baton.toml")
        task = next(t for t in load_tasks(cfg) if t.id == "data")
        res = run_template(CheckContext(cfg, task, cfg.results_dir / "data"))
        return res, res.markdown(task, cfg.results_dir)

    def test_main_path_passes_against_the_copy(self):
        res, report = self.check()
        self.assertEqual(res.gaps, [])
        self.assertIn("log in: POST /auth/login -> HTTP 200", report)
        self.assertIn("database: mongodb://db.local:27017/app_copy", report)
        self.assertNotIn("hunter2", report)                  # credentials are never printed
        self.assertNotIn("pw-from-env", report)

    def test_a_failure_shows_the_exact_response(self):
        self.configure(db_url="mongodb://u:p@db.local/app_nokey")
        res, report = self.check()
        [gap] = res.gaps
        self.assertIn("enroll through the invite link: POST /plans/p1/enroll -> HTTP 400, expected 200/201/204", gap)
        self.assertIn('Response: {"detail": "resource type \'person\' not found"}', gap)
        self.assertEqual(res.status, "blocked")

    def test_a_missing_snapshot_url_is_not_replaced_by_anything(self):
        self.configure(db_url=None)
        with mock.patch.dict(os.environ):
            os.environ.pop("BATON_SMOKE_DB_URL", None)
            res, _ = self.check()
        self.assertEqual(len(res.gaps), 1)
        self.assertIn("smoke_db_url is not set", res.gaps[0])
        self.assertIn("will not fall back to fixtures", res.gaps[0])

    def test_the_url_may_come_from_the_environment(self):
        self.configure(db_url=None)
        with mock.patch.dict(os.environ, {"BATON_SMOKE_DB_URL": REAL_DB}):
            self.assertEqual(self.check()[0].gaps, [])

    ENV_STEP = """
[[params.steps]]
name = "which database"
path = "/health"
expect_json = { "env.FAKE_HOST" = "db.local", "env.FAKE_PORT" = "27017", "env.FAKE_NAME" = "app_copy" }
"""

    def test_db_env_can_be_a_table_over_the_parts_of_the_url(self):
        self.configure(db_env={"FAKE_DB": "{url}", "FAKE_HOST": "{host}", "FAKE_PORT": "{port}",
                               "FAKE_NAME": "{db}"})
        res, report = self.check(self.ENV_STEP)
        self.assertEqual(res.gaps, [])
        self.assertIn("database: FAKE_DB=mongodb://db.local:27017/app_copy", report)
        self.assertNotIn("hunter2", report)
        self.assertIn("which database", report)

    def test_a_table_may_mix_literals_and_the_url_and_needs_no_url_when_all_literal(self):
        self.configure(db_url=None, db_env={"FAKE_DB": "mongodb://db.local:27017/app_copy",
                                            "FAKE_HOST": "db.local", "FAKE_PORT": "27017",
                                            "FAKE_NAME": "app_copy"})
        with mock.patch.dict(os.environ):
            os.environ.pop("BATON_SMOKE_DB_URL", None)
            res, report = self.check(self.ENV_STEP)
        self.assertEqual(res.gaps, [])
        self.assertIn("database: FAKE_DB=mongodb://db.local:27017/app_copy", report)

    def test_a_table_that_needs_the_url_says_so_when_it_is_missing(self):
        self.configure(db_url=None, db_env={"FAKE_DB": "{url}"})
        with mock.patch.dict(os.environ):
            os.environ.pop("BATON_SMOKE_DB_URL", None)
            [gap] = self.check()[0].gaps
        self.assertIn("db_env.FAKE_DB uses {url} but smoke_db_url is not set", gap)

    def test_an_unknown_placeholder_is_named(self):
        self.configure(db_env={"FAKE_DB": "{nope}"})
        [gap] = self.check()[0].gaps
        self.assertIn("unknown placeholder {nope}", gap)

    def test_secret_looking_variables_are_not_printed(self):
        self.configure(db_env={"FAKE_DB": "{url}", "FAKE_HOST": "{host}", "FAKE_PASSWORD": "{password}"})
        _, report = self.check()
        self.assertIn("FAKE_PASSWORD=***", report)
        self.assertNotIn("hunter2", report)

    def test_a_server_that_is_already_running_is_refused(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, str(FAKE_API), str(self.port)],
                                env={**os.environ, "FAKE_DB": "the dev database"})
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        from baton.checks.servers import reachable
        import time
        for _ in range(100):
            if reachable(f"http://127.0.0.1:{self.port}/health"):
                break
            time.sleep(0.1)
        [gap] = self.check()[0].gaps
        self.assertIn("already answers", gap)
        self.assertIn("allow_reuse = true", gap)

    def test_a_bad_login_stops_the_walk_there(self):
        with mock.patch.dict(os.environ, {"QA_PASSWORD": "wrong"}):
            res, _ = self.check()
        [gap] = res.gaps
        self.assertTrue(gap.startswith("log in: POST /auth/login -> HTTP 401"), gap)
        self.assertIn("bad login", gap)
        self.assertNotIn("wrong", gap)

    def test_a_capture_that_finds_nothing_is_a_gap(self):
        steps = STEPS.replace('capture = { token = "access_token" }', 'capture = { token = "data.token" }')
        [gap] = self.check(steps)[0].gaps
        self.assertIn("has no `data.token` to capture as token", gap)

    def test_prepare_cmd_runs_first_with_the_url_and_a_failure_blocks(self):
        marker = self.root / "prepared.txt"
        script = self.root / "prepare.sh"
        script.write_text(f'#!/bin/sh\necho "$SMOKE_DB_URL" > {marker}\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.configure(prepare_cmd=str(script))
        self.assertEqual(self.check()[0].gaps, [])
        self.assertEqual(marker.read_text().strip(), REAL_DB)
        script.write_text("#!/bin/sh\necho no dump found >&2\nexit 7\n")
        [gap] = self.check()[0].gaps
        self.assertIn("prepare_cmd failed (exit 7)", gap)
        self.assertIn("no dump found", gap)


class HardcodedKeyRunTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.add_task("a")

    def agent(self, *steps):
        self.scenario_is(claude=list(steps))

    def writes_source(self):
        """A fake step only appends a counter line, so a wrapper writes the real source before each call."""
        wrapper = self.root / "bin" / "claude"
        wrapper.write_text(
            "#!/bin/sh\ncase \"$*\" in *--help*) ;; *)\n"
            "mkdir -p app && printf 'def load(t):\\n    return t.type_key == \"person\"\\n' > app/svc.py ;; esac\n"
            f"exec {sys.executable} {Path(__file__).parent / 'fake_agent.py'} claude \"$@\"\n")

    def test_a_flagged_literal_sends_the_agent_back_once(self):
        self.writes_source()
        self.agent({"commit": "a.txt", "status": "done"}, {"status": "done"})
        self.assertEqual(self.run_baton(), 0)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("string literals that look like hardcoded data keys", calls[1]["prompt"])
        self.assertIn('app/svc.py:2: type_key = "person"', calls[1]["prompt"])
        self.assertIn("## Hardcoded keys", calls[1]["prompt"])
        # still there after the second pass, and unexplained: finished, but flagged
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("1 possible hardcoded data key(s)", self.state.tasks["a"].note)
        self.assertIn('type_key = "person"',
                      (self.run_dir / "results" / "a" / "hardcoded-keys.md").read_text())

    def test_an_explained_literal_is_accepted(self):
        self.writes_source()
        self.agent({"commit": "a.txt", "status": "done"},
                   {"status": "done", "result_extra": "\n## Hardcoded keys\n`person` is a protocol constant.\n"})
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.state.tasks["a"].note, "")
        self.assertFalse((self.run_dir / "results" / "a" / "hardcoded-keys.md").exists())

    def test_a_second_pass_that_leaves_the_result_file_keeps_the_first_verdict(self):
        self.writes_source()
        self.agent({"commit": "a.txt", "status": "done"}, {})
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("possible hardcoded data key", self.state.tasks["a"].note)

    def test_clean_work_finishes_in_one_attempt(self):
        self.agent({"commit": "a.txt", "status": "done"})
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.state.tasks["a"].note, "")

    def test_the_check_can_be_turned_off(self):
        self.write_config(hardcoded_key_check=False)
        self.writes_source()
        self.agent({"commit": "a.txt", "status": "done"})
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
