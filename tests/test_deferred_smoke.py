from baton.config import load_config
from baton.preflight import preflight
from tests.test_runner import RunnerFixture

UI = 'paths = ["src/app/join/page.tsx"]\n'
SECTIONS = "\n## User journey\nThe user joins.\n"


class DeferredSmokeTest(RunnerFixture):
    def plan(self):
        return preflight(load_config(self.run_dir / "baton.toml"))

    def add_ui(self, tid):
        self.add_task(tid, extra=UI)
        path = self.run_dir / "tasks" / f"{tid}.md"
        path.write_text(path.read_text() + SECTIONS)

    def warnings_about_browser(self, warnings):
        return [w for w in warnings if "browser" in w]

    def test_without_a_deferral_each_ui_task_warns(self):
        self.add_ui("a")
        self.add_ui("b")
        _, warnings, _ = self.plan()
        self.assertEqual(len(self.warnings_about_browser(warnings)), 2)

    def test_a_deferral_prints_one_summary_line(self):
        self.add_ui("a")
        self.add_ui("b")
        self.write_config(extra='[browser_smoke]\ndeferred = "Playwright not approved yet"\n')
        errors, warnings, _ = self.plan()
        self.assertEqual(errors, [])
        [line] = self.warnings_about_browser(warnings)
        self.assertIn("browser-smoke is deferred (Playwright not approved yet)", line)
        self.assertIn("2 UI task(s)", line)
        self.assertIn("a, b", line)

    def test_a_deferral_does_not_hide_the_missing_journey_warning(self):
        self.add_task("a", extra=UI)
        self.write_config(extra='[browser_smoke]\ndeferred = "later"\n')
        _, warnings, _ = self.plan()
        self.assertTrue(any("neither a" in w for w in warnings))

    def test_the_deferral_is_listed_under_not_verified(self):
        self.add_ui("a")
        self.write_config(extra='[browser_smoke]\ndeferred = "Playwright not approved yet"\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        text = (self.run_dir / "results" / "SUMMARY.md").read_text()
        self.assertIn("- **plan**: browser-smoke is deferred (Playwright not approved yet): no page was opened "
                      "in a browser for a", text)
        self.assertNotIn("UI task that no browser-smoke follows", text)
