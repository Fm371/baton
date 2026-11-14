"""The agents keep a progress file; the next agent gets it with the commits."""

from baton.config import load_config
from baton.isolation import hidden_paths
from baton.tasks import load_tasks
from tests.test_runner import RunnerFixture, claude_limit, codex_limit


class ProgressTest(RunnerFixture):
    def test_the_prompt_asks_for_a_progress_file(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        prompt = self.calls()[0]["prompt"]
        self.assertIn(f"keep {self.run_dir / 'results' / 'a.progress.md'} up to date", prompt)
        self.assertIn("plan, what is done, what is next, and open questions", prompt)
        self.assertNotIn("previous agent's notes", prompt)

    def test_the_notes_travel_to_the_next_provider_on_a_switch(self):
        self.add_task("a")
        limited = {**claude_limit(), "commit": "a.txt",
                   "progress": "PLAN: 1 model, 2 api, 3 tests\nDONE: model\nNEXT: api\nOPEN: pagination size?"}
        self.scenario_is(claude=[limited], codex=[{"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        prompt = self.calls()[1]["prompt"]
        self.assertEqual(self.calls()[1]["provider"], "codex")
        self.assertIn("<previous-agent-notes>", prompt)
        self.assertIn("NEXT: api", prompt)
        self.assertIn("OPEN: pagination size?", prompt)

    def test_codex_notes_are_written_in_its_task_folder_and_promoted(self):
        self.write_config(start_provider="codex")
        self.add_task("a")
        results = self.run_dir / "results"
        self.scenario_is(codex=[{**codex_limit(), "dirty": "a.txt", "progress": "NEXT: wire the route"}],
                         claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn(f"keep {results / 'a' / 'codex-progress.md'} up to date", self.calls()[0]["prompt"])
        self.assertEqual((results / "a.progress.md").read_text(), "NEXT: wire the route")
        self.assertIn("NEXT: wire the route", self.calls()[1]["prompt"])

    def test_a_new_start_of_a_finished_task_drops_the_old_notes(self):
        self.add_task("a")
        results = self.run_dir / "results"
        results.mkdir()
        (results / "a.progress.md").write_text("OLD NOTES FROM AN EARLIER RUN")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertFalse((results / "a.progress.md").exists())
        self.assertNotIn("OLD NOTES", self.calls()[0]["prompt"])

    def test_a_verifiers_notes_are_hidden_from_implementers(self):
        self.add_task("a")
        self.add_verifier("va")
        cfg = load_config(self.run_dir / "baton.toml")
        tasks = load_tasks(cfg)
        impl = next(t for t in tasks if t.id == "a")
        hidden = [str(p) for p in hidden_paths(cfg, impl, [t for t in tasks if t.is_verifier])]
        self.assertIn(str((self.run_dir / "results" / "va.progress.md").resolve()), hidden)

    def test_a_verifier_can_write_its_own_result_but_not_read_others(self):
        self.add_task("a")
        self.add_verifier("va")
        results = self.run_dir / "results"
        (results / "a").mkdir(parents=True)
        (results / "a.md").write_text("STATUS: done")
        (results / "SUMMARY.md").write_text("summary")
        (results / "va").mkdir()
        cfg = load_config(self.run_dir / "baton.toml")
        tasks = load_tasks(cfg)
        ver = next(t for t in tasks if t.id == "va")
        hidden = {str(p) for p in hidden_paths(cfg, ver, [ver])}
        r = results.resolve()
        for name in ("a.md", "a", "SUMMARY.md"):
            self.assertIn(str(r / name), hidden)
        for name in ("va.md", "va.progress.md", "va"):
            self.assertNotIn(str(r / name), hidden)
        self.assertNotIn(str(r), hidden)
        self.assertIn(str((self.run_dir / "tasks").resolve()), hidden)
