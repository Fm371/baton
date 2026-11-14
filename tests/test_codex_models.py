import json

from baton.config import load_config
from baton.preflight import preflight
from tests.test_runner import RunnerFixture


class CodexModelTest(RunnerFixture):
    def cache(self, models):
        home = self.root / "empty-home"
        home.mkdir(exist_ok=True)
        (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": m} for m in models]}))

    def plan(self):
        return preflight(load_config(self.run_dir / "baton.toml"))

    def test_unknown_codex_model_fails_the_plan_with_a_suggestion(self):
        self.cache(["gpt-5.6-codex", "gpt-5.6"])
        self.add_task("a")
        (self.run_dir / "baton.toml").write_text(
            (self.run_dir / "baton.toml").read_text().replace('model = "sol"', 'model = "gpt-5.6-codx"'))
        errors, _, _ = self.plan()
        self.assertEqual(len(errors), 1)
        self.assertIn("rung r3: codex model 'gpt-5.6-codx' is not known", errors[0])
        self.assertIn("gpt-5.6-codex", errors[0])

    def test_known_model_passes(self):
        self.cache(["sol"])
        self.add_task("a")
        errors, warnings, _ = self.plan()
        self.assertEqual(errors, [])
        self.assertFalse([w for w in warnings if "model ids" in w])

    def test_known_models_in_the_config_take_precedence(self):
        self.cache(["sol"])
        self.add_task("a")
        text = (self.run_dir / "baton.toml").read_text().replace(
            "[providers.codex]\n", '[providers.codex]\nknown_models = ["gpt-5.6"]\n')
        (self.run_dir / "baton.toml").write_text(text)
        errors, _, _ = self.plan()
        self.assertTrue(any("'sol' is not known" in e for e in errors))

    def test_without_a_cache_the_ids_cannot_be_checked_and_baton_says_so(self):
        (self.root / "empty-home" / "models_cache.json").unlink()
        self.add_task("a")
        errors, warnings, _ = self.plan()
        self.assertEqual(errors, [])
        self.assertTrue(any("cannot check model ids" in w for w in warnings))

    def test_a_task_that_cannot_use_codex_is_not_checked(self):
        self.cache(["gpt-5.6"])
        self.add_task("a", extra='providers = ["claude"]\n')
        errors, _, _ = self.plan()
        self.assertEqual(errors, [])
