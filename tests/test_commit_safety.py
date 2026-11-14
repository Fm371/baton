"""Commit safety: baton never commits on a branch that is not the task's, keeps only certain secrets out
of WIP commits (and out of the way), leaves a repository as the suite found it, and does not count
commits for a task that never started."""

import tempfile
import unittest
from pathlib import Path

from baton import gitutil
from baton.checks import CheckContext, run_template
from baton.config import load_config
from baton.runner import Runner
from baton.tasks import load_tasks
from tests.test_branches import write_task
from tests.test_runner import FakeClock, RunnerFixture, git


class UnpreparedTaskTest(RunnerFixture):
    def test_a_dirty_repository_is_never_committed_on_the_users_branch(self):
        write_task(self, "a", branch="feat/x")
        cfg = load_config(self.run_dir / "baton.toml")
        [task] = load_tasks(cfg)
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        (self.repo / "notes.txt").write_text("my half-written thoughts\n")      # the user's, on develop
        self.assertIsNone(runner._start_task(task))
        ts = runner.state.task("a")
        self.assertEqual(ts.status, "failed")
        self.assertIn("cannot prepare repository", ts.note)
        self.assertEqual(ts.commits, 0)                                         # not the whole history
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "init")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "?? notes.txt")
        self.assertIn("(0 commits", (cfg.logs_dir / "run.log").read_text())

    def test_wip_is_only_committed_on_the_tasks_own_branch(self):
        write_task(self, "a", branch="feat/x")
        cfg = load_config(self.run_dir / "baton.toml")
        [task] = load_tasks(cfg)
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        (self.repo / "mine.txt").write_text("x\n")                              # on develop, not feat/x
        runner.commit_leftovers(task, "the run was stopped")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "init")
        self.assertIn("not on feat/x", (cfg.logs_dir / "run.log").read_text())

    def test_recover_leaves_a_repository_on_another_branch_alone(self):
        write_task(self, "a", branch="feat/x")
        cfg = load_config(self.run_dir / "baton.toml")
        tasks = load_tasks(cfg)
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        runner.state.task("a").status = "running"
        (self.repo / "mine.txt").write_text("x\n")
        runner.recover(tasks)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "init")


class SecretsInARunTest(RunnerFixture):
    def test_secrets_py_is_ordinary_code_and_gets_committed(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"dirty": "secrets.py", "content": "NAME = 'x'\n", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "show", "feat/x:secrets.py"), "NAME = 'x'")
        self.assertEqual(self.state.task("a").commits, 1)

    def test_a_refused_env_file_is_excluded_so_the_queue_goes_on_and_the_task_says_so(self):
        write_task(self, "a", branch="feat/x")
        write_task(self, "b", branch="feat/y")
        self.scenario_is(claude=[{"dirty": ".env", "content": "TOKEN=abc\n", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        a = self.state.task("a")
        self.assertEqual(a.status, "done")
        self.assertIn("not committed because they look like secrets: .env", a.note)
        self.assertIn(".env", (self.repo / ".git" / "info" / "exclude").read_text())
        self.assertEqual(self.state.task("b").status, "done")                   # the repository was not left dirty
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue((self.repo / ".env").exists())                          # the file itself is untouched

    def test_a_private_key_is_refused_whatever_it_is_called(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "develop")
        (repo / "deploy.txt").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
        (repo / "notes.txt").write_text("hello\n")
        self.assertEqual(gitutil.secret_looking(repo), ["deploy.txt"])

    def test_a_renamed_file_is_listed_under_its_new_name(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "develop")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        (repo / "old.txt").write_text("some content that is long enough to be a rename\n" * 5)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "mv", "old.txt", ".env")
        (repo / "other.txt").write_text("x\n")
        self.assertEqual(sorted(gitutil.changed_files(repo)), [".env", "other.txt"])
        self.assertEqual(gitutil.secret_looking(repo), [".env"])


class SuiteLeavesTheRepositoryAsFoundTest(RunnerFixture):
    def check(self, params=""):
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        (self.run_dir / "verifiers" / "suite.md").write_text(
            f'+++\nkind = "verify"\ntemplate = "repo-suite"\nrepo = "../repo"\n{params}+++\n')
        cfg = load_config(self.run_dir / "baton.toml")
        task = next(t for t in load_tasks(cfg) if t.id == "suite")
        res = run_template(CheckContext(cfg, task, cfg.results_dir / "suite"))
        return res, res.markdown(task, cfg.results_dir)

    def test_files_the_suite_created_are_removed_and_the_users_are_kept(self):
        self.write_config(extra='[repos."../repo"]\n'
                                'suite_cmd = "mkdir -p reports && touch coverage.xml reports/junit.xml && echo 3 passed"\n')
        (self.repo / "mine.txt").write_text("the user's\n")
        res, report = self.check()
        self.assertEqual(res.gaps, [])
        self.assertFalse((self.repo / "coverage.xml").exists())
        self.assertFalse((self.repo / "reports").exists())
        self.assertTrue((self.repo / "mine.txt").exists())
        self.assertIn("baton removed them: coverage.xml, reports/junit.xml", report)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "?? mine.txt")

    def test_the_branch_the_repository_was_on_comes_back(self):
        git(self.repo, "branch", "other")
        self.write_config(extra='[repos."../repo"]\nsuite_cmd = "touch coverage.xml && echo ok"\n')
        res, report = self.check('[params]\nbranch = "other"\n')
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertIn("put back on 'develop'", report)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


class StaleInitialBranchTest(RunnerFixture):
    def test_a_branch_the_user_moved_to_is_not_switched_away_from(self):
        write_task(self, "a", branch="feat/x")
        cfg = load_config(self.run_dir / "baton.toml")
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        runner.ordered = load_tasks(cfg)
        runner.state.initial_branches[str(self.repo)] = "develop"       # recorded by a run that was killed
        git(self.repo, "checkout", "-q", "-b", "mine")                  # then the user went elsewhere
        runner.restore_branches()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "mine")
        self.assertEqual(runner.state.initial_branches, {})
        self.assertIn("which baton did not choose", (cfg.logs_dir / "run.log").read_text())


if __name__ == "__main__":
    unittest.main()
