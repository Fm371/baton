"""Codex gets its own task's results folder only, and never runs a verifier."""


from baton.config import load_config
from baton.preflight import preflight
from tests.test_runner import RunnerFixture


class CodexScopeTest(RunnerFixture):
    def mounted(self, call):
        argv = call["argv"]
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]

    def test_codex_mounts_only_its_task_folder_not_results(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.add_verifier("va", deps=["a"])
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}],
                         claude=[{"commit": "test_a.py", "status": "done"}])
        self.run_baton()
        results = self.run_dir / "results"
        codex_calls = [c for c in self.calls() if c["provider"] == "codex"]
        self.assertEqual(self.mounted(codex_calls[0]), [str(results / "a")])
        self.assertEqual(self.mounted(codex_calls[1]), [str(results / "b")])
        for c in codex_calls:
            self.assertNotIn(str(results), self.mounted(c))

    def test_codex_writes_its_result_in_the_task_folder_and_baton_promotes_it(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "done", "commit_message": "feat(a): add a"}])
        self.run_baton()
        results = self.run_dir / "results"
        self.assertIn(f"write your result file to {results / 'a' / 'codex-result.md'}", self.calls()[0]["prompt"])
        self.assertTrue((results / "a.md").read_text().startswith("STATUS: done"))
        self.assertEqual(self.state.tasks["a"].status, "done")

    def test_a_stale_codex_result_is_not_promoted(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        stale = self.run_dir / "results" / "a" / "codex-result.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("STATUS: done\nCOMMIT: stale\n")
        import os
        os.utime(stale, (1, 1))
        self.scenario_is(codex=[{"dirty": "a.txt", "exit": 1, "stderr": "boom"}] * 3)
        self.run_baton()
        self.assertFalse((self.run_dir / "results" / "a.md").exists())

    def test_preflight_rejects_a_verifier_that_may_run_on_codex(self):
        self.add_task("a")
        self.add_verifier("va", extra='providers = ["claude", "codex"]\n')
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertEqual(len(errors), 1)
        self.assertIn("verifier va", errors[0])
        self.assertIn("includes codex", errors[0])

    def test_preflight_rejects_codex_in_verifier_providers(self):
        self.write_config(verifier_providers=["claude", "codex"])
        self.add_task("a")
        self.add_verifier("va")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("run.verifier_providers includes codex" in e for e in errors), errors)

    def test_a_claude_verifier_still_passes_preflight(self):
        self.add_task("a")
        self.add_verifier("va")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertEqual(errors, [])
