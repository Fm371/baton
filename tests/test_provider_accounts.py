"""Runs with several account names, shared CLI binaries, and separate homes."""

import shlex

from baton.config import load_config
from baton.ops import providers_reset
from baton.preflight import plan_text, preflight
from baton.state import load_state
from baton.watch import describe
from tests.test_review import capture, codex_says
from tests.test_runner import RunnerFixture, codex_limit
from tests.test_start_provider import JumpClock


class ProviderAccountTest(RunnerFixture):
    @property
    def config_path(self):
        return self.run_dir / "baton.toml"

    def account(self, name, kind):
        home = self.run_dir / "homes" / name
        wrapper = self.root / "bin" / name
        env = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}[kind]
        wrapper.write_text(
            f'#!/bin/sh\ntest "${env}" = {shlex.quote(str(home))} || exit 98\n'
            f'exec {shlex.quote(str(self.root / "bin" / kind))} "$@"\n'
        )
        wrapper.chmod(0o755)
        models = 'known_models = ["sol", "alternate"]\n' if kind == "codex" else ""
        return f'\n[providers.{name}]\ntype = "{kind}"\nhome = "homes/{name}"\nbin = "{wrapper}"\n{models}'

    def test_handoffs_keep_account_limits_and_return_to_business_after_reset(self):
        accounts = self.account("business", "codex") + self.account("personal", "codex")
        self.write_config(accounts, start_provider="", provider_order=["business", "personal", "claude"],
                          max_limit_cycles=1)
        self.config_path.write_text(self.config_path.read_text().replace(
            '[providers.codex]\n', '[providers.codex]\nenabled = false\n').replace(
            'codex = { model = "sol", effort = "medium" }',
            'codex = { model = "sol", effort = "medium" }\npersonal = { model = "alternate" }'))
        self.add_task("a")
        self.add_task("b", deps=["a"], extra='providers = ["codex"]\n')
        self.scenario_is(codex=[codex_limit("10:30 PM"), codex_limit("1:30 AM"),
                                {"dirty": "b.txt", "status": "done"}],
                         claude=[{"commit": "a.txt", "status": "done"}])

        cfg = load_config(self.config_path)
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertIn("providers: business > personal > claude", plan_text(cfg, tasks))
        self.assertEqual(self.run_baton(clock=JumpClock(self, 3)), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["business", "personal", "claude"])
        self.assertEqual(self.state.tasks["b"].providers, ["business"])
        self.assertEqual(self.metrics[1]["model"], "alternate")
        self.assertIsNotNone(self.state.providers["personal"]["limited_until"])
        self.assertIsNone(self.state.providers["business"]["limited_until"])
        out_path = cfg.results_dir / "a" / "attempt-02.personal.out"
        self.assertTrue(out_path.is_file())
        self.assertEqual(describe(out_path), "task a, attempt 2 on personal")
        providers_reset(cfg, "personal")
        self.assertIsNone(load_state(cfg.state_dir / "run.json").providers["personal"]["limited_until"])

    def test_named_claude_uses_its_own_home_and_cli(self):
        self.write_config(self.account("claude-work", "claude"), start_provider="claude-work")
        self.add_task("a", extra='providers = ["claude-work"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])

        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].providers, ["claude-work"])
        self.assertIn("--settings", self.calls()[0]["argv"])

    def test_verifier_restrictions_apply_to_named_codex(self):
        self.write_config(self.account("business", "codex"), verifier_providers=["business"])
        self.add_task("a")
        self.add_verifier("checks")

        errors, _, _ = preflight(load_config(self.config_path))
        self.assertTrue(any("run.verifier_providers includes codex" in error for error in errors), errors)

    def test_named_codex_can_review_in_a_read_only_sandbox(self):
        self.write_config(self.account("business", "codex"), provider_order=["claude", "business"])
        self.add_task("a", extra="review = true\n")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}],
                         codex=[codex_says("VERDICT: approve")])
        self.assertEqual(self.run_baton(), 0)

        rc, out, err = capture(["review", "--only", "a", "-c", str(self.config_path)])
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("business", out)
        args = self.calls()[-1]["argv"]
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
