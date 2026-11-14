"""Codex work is committed by baton, with the repository's hooks."""

from tests.test_runner import RunnerFixture, git


class CodexHookTest(RunnerFixture):
    def set_hook(self, body):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\n{body}\n")
        hook.chmod(0o755)

    def test_hooks_run_on_a_codex_commit_with_a_commit_line(self):
        self.write_config(start_provider="codex")
        self.set_hook(f"echo ran >> {self.root}/hook.log")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "done", "commit_message": "feat(a): add a"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual((self.root / "hook.log").read_text(), "ran\n")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"), "feat(a): add a")

    def test_a_refused_codex_commit_is_kept_as_wip_and_the_task_is_partial(self):
        self.write_config(start_provider="codex")
        self.set_hook("echo 'commitlint: type must be one of' >&2; exit 1")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "done", "commit_message": "bad message"}])
        self.assertEqual(self.run_baton(), 0)
        ts = self.state.tasks["a"]
        self.assertEqual(ts.status, "partial")
        self.assertEqual(ts.commits, 1)
        self.assertIn("commit hook failed", ts.note)
        self.assertIn("commitlint: type must be one of", ts.note)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"),
                         "chore(a): wip, commit hook failed (unverified)")

    def test_unfinished_codex_work_is_still_committed_without_hooks(self):
        self.write_config(start_provider="codex")
        self.set_hook("exit 1")
        self.add_task("a")
        self.scenario_is(codex=[{"dirty": "a.txt", "status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "partial")
        self.assertNotIn("hook", self.state.tasks["a"].note)
        self.assertIn("wip", git(self.repo, "log", "-1", "--format=%s", "feat/x"))
