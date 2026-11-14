"""`baton plan` says so when a rung uses an xhigh effort or an astra-class model."""

from baton.config import load_config
from baton.preflight import preflight
from tests.test_runner import RunnerFixture


class HeavyModelWarningTest(RunnerFixture):
    def warnings(self):
        cfg = load_config(self.run_dir / "baton.toml")
        return [w for w in preflight(cfg, create_dirs=False)[1] if "heavy settings" in w]

    def edit(self, old, new):
        p = self.run_dir / "baton.toml"
        p.write_text(p.read_text().replace(old, new, 1))

    def test_the_default_fixture_is_quiet(self):
        self.add_task("a")
        self.assertEqual(self.warnings(), [])

    def test_an_xhigh_effort_is_warned_about_once_per_provider(self):
        self.add_task("a")
        self.edit('claude = { model = "sonnet", effort = "medium" }', 'claude = { model = "opus", effort = "xhigh" }')
        ws = self.warnings()
        self.assertEqual(len(ws), 1, ws)
        self.assertIn("claude: heavy settings in r3 (opus xhigh)", ws[0])

    def test_an_astra_class_codex_model_is_warned_about(self):
        self.add_task("a")
        self.edit('codex = { model = "sol", effort = "medium" }', 'codex = { model = "gpt-6-astra", effort = "high" }')
        ws = self.warnings()
        self.assertEqual(len(ws), 1, ws)
        self.assertIn("codex: heavy settings in r3 (gpt-6-astra high)", ws[0])

    def test_the_shipped_example_has_no_heavy_rung(self):
        from pathlib import Path
        from baton.preflight import heavy_model_warnings
        cfg = load_config(Path(__file__).resolve().parent.parent / "baton" / "templates" / "task-auto" / "baton.toml")
        self.assertEqual(heavy_model_warnings(cfg, cfg.enabled_providers()), [])
