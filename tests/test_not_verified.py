"""The "Not verified" section of SUMMARY.md."""

import json
import unittest

from baton.notverified import markdown, section_items
from tests.test_runner import RunnerFixture


class SectionItemsTest(unittest.TestCase):
    def test_bullets_become_items_and_none_is_nothing(self):
        self.assertEqual(section_items("- payment: mocked gateway\n* email never sent\n"),
                         ["payment: mocked gateway", "email never sent"])
        self.assertEqual(section_items("None."), [])
        self.assertEqual(section_items(None), [])
        self.assertEqual(section_items("only the unit tests\nran"), ["only the unit tests ran"])

    def test_markdown_lists_each_who_and_says_when_empty(self):
        text = markdown([("a", ["x", "y"]), ("b", ["z"])])
        self.assertIn("- **a**: x\n  - y\n- **b**: z", text)
        self.assertIn("Nothing declared.", markdown([]))


class CheckReportTest(RunnerFixture):
    def test_a_built_in_checks_not_checked_section_is_listed(self):
        from baton.config import load_config
        from baton.notverified import reported
        from baton.tasks import Task
        cfg = load_config(self.run_dir / "baton.toml")
        cfg.results_dir.mkdir(exist_ok=True)
        (cfg.results_dir / "smoke.md").write_text(
            "STATUS: done\n\n## Gaps (0)\n\nNone.\n\n## Not checked\n\n- API host (api_origins is not set)\n"
            "- route /join: no screenshot\n\n## Files\n")
        t = Task(id="smoke", path=cfg.results_dir, repo=cfg.base_dir, body="", kind="verify", template="browser-smoke")
        self.assertEqual(reported(cfg.results_dir, t),
                         ["API host (api_origins is not set)", "route /join: no screenshot"])


class NotVerifiedRunTest(RunnerFixture):
    def summary(self):
        return (self.run_dir / "results" / "SUMMARY.md").read_text()

    def test_the_prompt_asks_for_the_section(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertIn("`## Not verified` section", self.calls()[0]["prompt"])

    def test_summary_and_status_json_list_what_the_agent_reported(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[
            {"commit": "a.txt", "status": "done",
             "result_extra": "\n## Not verified\n- payment gateway: mocked\n- email: not sent\n"},
            {"commit": "b.txt", "status": "done", "result_extra": "\n## Not verified\nNone.\n"}])
        self.run_baton()
        text = self.summary()
        self.assertIn("## Not verified", text)
        self.assertIn("- **a**: payment gateway: mocked\n  - email: not sent", text)
        self.assertNotIn("**b**", text)
        status = json.loads((self.run_dir / "results" / "a" / "status.json").read_text())
        self.assertEqual(status["not_verified"], ["payment gateway: mocked", "email: not sent"])

    def test_a_ui_task_without_browser_smoke_is_listed(self):
        self.add_task("a", extra='paths = ["src/app/join/page.tsx"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertIn("- **a**: UI task that no browser-smoke follows", self.summary())

    def test_nothing_declared_is_said_plainly(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertIn("Nothing declared.", self.summary())


if __name__ == "__main__":
    unittest.main()
