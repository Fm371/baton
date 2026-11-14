"""Branch handling, WIP commits and re-runs: a task with no branch, dependency bases, the user's
checkout put back, secrets kept out of WIP commits, a clean error when a commit fails."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from baton import gitutil
from baton.config import load_config
from baton.preflight import plan_text, preflight
from baton.runner import Runner
from baton.tasks import load_tasks
from tests.test_runner import FakeClock, RunnerFixture, git


def write_task(fx, tid, branch=None, deps=(), base=None):
    meta = ['repo = "../repo"', 'work_type = "feature"']
    if branch:
        meta.append(f'branch = "{branch}"')
    if base:
        meta.append(f'base = "{base}"')
    if deps:
        meta.append("depends_on = [" + ", ".join(f'"{d}"' for d in deps) + "]")
    (fx.run_dir / "tasks" / f"{tid}.md").write_text("+++\n" + "\n".join(meta) + f"\n+++\nBuild {tid}.\n")


class AutomaticBranchTest(RunnerFixture):
    def test_a_task_without_a_branch_gets_baton_id_and_the_checkout_comes_back(self):
        write_task(self, "a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(tasks[0].branch, "baton/a")
        text = plan_text(cfg, tasks)
        self.assertIn("baton/a [automatic] from develop", text)
        self.assertIn("goes back to the branch it is on now: repo -> develop", text)
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual(git(self.repo, "rev-list", "--count", "develop"), "1")      # develop is untouched
        self.assertEqual(git(self.repo, "rev-list", "--count", "develop..baton/a"), "1")
        self.assertIn("back on develop", self.log)
        self.assertEqual(self.state.initial_branches, {})

    def test_without_a_default_base_it_starts_from_the_current_checkout(self):
        cfg_file = self.run_dir / "baton.toml"
        cfg_file.write_text(cfg_file.read_text().replace('default_base = "develop"\n', ""))
        git(self.repo, "checkout", "-q", "-b", "work")
        (self.repo / "w.txt").write_text("w\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "work commit")
        write_task(self, "a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(cfg_file)
        _, _, tasks = preflight(cfg)
        self.assertIn("from the current checkout (work)", plan_text(cfg, tasks))
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "work")
        self.assertEqual(git(self.repo, "rev-list", "--count", "work..baton/a"), "1")
        self.assertEqual(git(self.repo, "show", "baton/a:w.txt"), "w")

    def test_a_dirty_repository_is_left_where_it_is(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.write_config(commit_leftovers=False)
        cfg = load_config(self.run_dir / "baton.toml")
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        runner.ordered = load_tasks(cfg)
        runner.state.initial_branches[str(self.repo)] = "develop"
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        (self.repo / "dirty.txt").write_text("x\n")
        runner.restore_branches()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feat/x")
        self.assertIn("left on feat/x", (cfg.logs_dir / "run.log").read_text())


class DependencyBaseTest(RunnerFixture):
    def test_a_dependent_task_starts_from_its_dependency_branch(self):
        write_task(self, "a", branch="feat/a")
        write_task(self, "b", branch="feat/b", deps=["a"])
        cfg = load_config(self.run_dir / "baton.toml")
        by_id = {t.id: t for t in load_tasks(cfg)}
        self.assertEqual(by_id["b"].base, "feat/a")
        self.assertEqual(by_id["a"].base, "develop")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "show", "feat/b:a.txt").split()[0], "claude")      # b sees a's work
        self.assertEqual(git(self.repo, "rev-list", "--count", "feat/a..feat/b"), "1")

    def test_an_explicit_base_wins(self):
        write_task(self, "a", branch="feat/a")
        write_task(self, "b", branch="feat/b", deps=["a"], base="develop")
        by_id = {t.id: t for t in load_tasks(load_config(self.run_dir / "baton.toml"))}
        self.assertEqual(by_id["b"].base, "develop")

    def test_the_same_branch_changes_nothing(self):
        write_task(self, "a", branch="feat/x")
        write_task(self, "b", branch="feat/x", deps=["a"])
        by_id = {t.id: t for t in load_tasks(load_config(self.run_dir / "baton.toml"))}
        self.assertEqual(by_id["b"].base, "develop")

    def test_dependencies_on_different_branches_warn(self):
        write_task(self, "a", branch="feat/a")
        write_task(self, "b", branch="feat/b")
        write_task(self, "c", branch="feat/c", deps=["a", "b"])
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("task c: depends on tasks on different branches" in w for w in warnings), warnings)


class RunAgainTest(RunnerFixture):
    def test_a_failed_task_run_again_starts_over(self):
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"exit": 0, "dirty": "h1.txt"}, {"exit": 0, "dirty": "h2.txt"},
                                 {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertEqual(self.state.tasks["a"].attempts, 2)
        first_files = sorted(p.name for p in (self.run_dir / "results" / "a").glob("attempt-*"))
        self.assertTrue(first_files)
        self.assertEqual(self.run_baton(only="a"), 0)
        ts = self.state.tasks["a"]
        self.assertEqual(ts.status, "done")
        self.assertEqual(ts.attempts, 1)
        self.assertEqual(ts.last_end, "")
        self.assertNotIn("started before", self.calls()[-1]["prompt"])
        earlier = sorted(p.name for p in (self.run_dir / "results" / "a" / "earlier-1").iterdir())
        self.assertEqual(earlier, first_files)
        self.assertTrue((self.run_dir / "results" / "a" / "attempt-01.claude.out").exists())


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q", "-b", "develop")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "README").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def test_names_that_look_like_secrets(self):
        for name in (".env", ".env.local", "app/.env.production", "deploy/server.pem", "id_rsa", "id_ed25519.pub",
                     "store.p12"):
            self.assertTrue(gitutil.looks_secret(name), name)
        # ordinary code or data that only has a suspicious name is committed like any other file
        for name in (".env.example", ".env.sample", "src/environment.py", "keys.md", "README", "secrets.py",
                     "credentials.json", "secrets.yaml", "x.key", ".npmrc"):
            self.assertFalse(gitutil.looks_secret(name), name)

    def test_a_wip_commit_leaves_secrets_out(self):
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / ".env").write_text("TOKEN=abc\n")
        (self.repo / ".env.example").write_text("TOKEN=\n")
        h = gitutil.commit_all(self.repo, "chore(a): wip")
        self.assertTrue(h)
        files = git(self.repo, "ls-tree", "-r", "--name-only", "HEAD").split()
        self.assertIn("a.txt", files)
        self.assertIn(".env.example", files)
        self.assertNotIn(".env", files)
        self.assertTrue((self.repo / ".env").exists())            # the file itself is left alone

    def test_only_secrets_left_means_no_commit(self):
        (self.repo / ".env").write_text("TOKEN=abc\n")
        self.assertIsNone(gitutil.commit_all(self.repo, "chore(a): wip"))
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "1")

    def test_large_files_are_listed(self):
        (self.repo / "big.bin").write_bytes(b"0" * 2048)
        (self.repo / "small.txt").write_text("s\n")
        self.assertEqual(gitutil.big_files(self.repo, limit=1024), ["big.bin"])


class IdentityTest(RunnerFixture):
    def test_preflight_names_a_missing_git_identity(self):
        write_task(self, "a", branch="feat/x")
        git(self.repo, "config", "--unset", "user.name")
        git(self.repo, "config", "--unset", "user.email")
        home = tempfile.mkdtemp()
        env = {"HOME": home, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(gitutil.identity_missing(self.repo), ["user.name", "user.email"])
            errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("no user.name or user.email" in e for e in errors), errors)


class CommitFailureTest(RunnerFixture):
    def test_a_failed_wip_commit_is_logged_and_named_in_the_next_failure(self):
        write_task(self, "a", branch="feat/x")
        write_task(self, "b", branch="feat/y")
        cfg = load_config(self.run_dir / "baton.toml")
        tasks = load_tasks(cfg)
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        git(self.repo, "checkout", "-q", "-b", "feat/x")        # the task's own branch: only there is WIP committed
        with mock.patch.object(gitutil, "commit_all", side_effect=gitutil.GitError("index.lock exists")):
            runner.commit_leftovers(tasks[0], "the run was stopped")
        self.assertIn("ERROR could not commit leftovers", (cfg.logs_dir / "run.log").read_text())
        self.assertIn(str(tasks[0].repo), runner.commit_failed)
        (self.repo / "left.txt").write_text("x\n")                      # what the failed commit left behind
        self.assertIsNone(runner._start_task(tasks[1]))
        note = runner.state.task("b").note
        self.assertIn("uncommitted changes", note)
        self.assertIn("WIP commit that should have cleaned it up failed: index.lock exists", note)


class InsideTheRepositoryTest(unittest.TestCase):
    def test_baton_folders_inside_a_task_repository_must_be_ignored(self):
        root = Path(tempfile.mkdtemp())
        work = root / "work"
        (work / "tasks").mkdir(parents=True)
        (work / "common.md").write_text("rules\n")
        bins = root / "bin"
        bins.mkdir()
        for name in ("claude", "codex"):
            p = bins / name
            p.write_text("#!/bin/sh\nexit 0\n")
            p.chmod(0o755)
        (work / "baton.toml").write_text(f"""
[run]
default_base = "develop"
common_prompt = "common.md"
[providers.claude]
bin = "{bins}/claude"
home = "{root}"
[providers.codex]
enabled = false
[routing.rungs.r3]
claude = {{ model = "sonnet", effort = "medium" }}
[routing.work_types]
feature = "r3"
""")
        (work / "tasks" / "a.md").write_text(
            '+++\nrepo = "."\nwork_type = "feature"\nbranch = "feat/x"\n+++\nBuild a.\n')
        git(work, "init", "-q", "-b", "develop")
        git(work, "config", "user.email", "t@example.com")
        git(work, "config", "user.name", "t")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "init")
        cfg = load_config(work / "baton.toml")
        errors, _, _ = preflight(cfg)
        self.assertTrue(any("git does not ignore it" in e for e in errors), errors)
        self.assertTrue(any("results/" in e for e in errors), errors)
        (work / ".gitignore").write_text("results/\nstate/\nlogs/\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "ignore baton folders")
        errors, _, _ = preflight(cfg)
        self.assertFalse(any("git does not ignore it" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
