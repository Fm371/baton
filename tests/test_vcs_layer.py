"""The low-level repository layer: repositories that are a folder inside a bigger one, the first entry of
`status`, merges in progress, submodules, file names that are not UTF-8, tags spelled like branches, and
commands that never return."""

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from baton import gitutil
from tests.test_branches import write_task
from tests.test_runner import RunnerFixture, git


def make_repo(path: Path, branch="develop") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    return path


def commit_file(repo: Path, name: str, text="x\n", message="add") -> None:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def tree_files(repo: Path, ref="HEAD") -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()


class SubfolderRepoTest(unittest.TestCase):
    def setUp(self):
        self.top = make_repo(Path(tempfile.mkdtemp()) / "mono")
        commit_file(self.top, "api/app.py", "print(1)\n", "init")
        self.sub = self.top / "api"

    def test_a_secret_in_a_subfolder_repo_is_not_committed(self):
        (self.sub / ".env").write_text("TOKEN=abc\n")
        (self.top / "root.txt").write_text("outside the folder\n")
        self.assertEqual(sorted(gitutil.changed_files(self.sub)), ["api/.env", "root.txt"])
        self.assertEqual(gitutil.secret_looking(self.sub), ["api/.env"])
        h = gitutil.commit_all(self.sub, "chore(a): wip")
        self.assertTrue(h)
        self.assertNotIn("api/.env", tree_files(self.top))
        self.assertIn("root.txt", tree_files(self.top))                 # the whole repository is committed
        self.assertFalse(gitutil.is_dirty(self.sub))                    # and left clean: .env is excluded
        self.assertIn("/api/.env", (self.top / ".git" / "info" / "exclude").read_text())
        self.assertTrue((self.sub / ".env").exists())

    def test_a_key_file_and_a_big_file_are_found_from_the_subfolder(self):
        (self.sub / "deploy.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        (self.sub / "big.bin").write_bytes(b"0" * 5000)
        self.assertEqual(gitutil.secret_looking(self.sub), ["api/deploy.txt"])
        self.assertEqual(gitutil.big_files(self.sub, limit=1024), ["api/big.bin"])

    def test_the_subfolder_is_named_in_preflight_and_by_subfolder_of(self):
        self.assertEqual(gitutil.subfolder_of(self.sub), self.top.resolve())
        self.assertIsNone(gitutil.subfolder_of(self.top))


class FirstEntryTest(unittest.TestCase):
    def test_the_first_modified_file_keeps_its_whole_name(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        commit_file(repo, ".env", "TOKEN=1\n", "tracked env")
        commit_file(repo, "z.txt")
        (repo / "a.txt").write_text("changed\n")            # a work-tree-only change: " M a.txt"
        self.assertEqual(gitutil.changed_files(repo), ["a.txt"])
        (repo / ".env").write_text("TOKEN=2\n")
        (repo / "z.txt").write_text("changed\n")
        files = gitutil.changed_files(repo)
        self.assertEqual(sorted(files), [".env", "a.txt", "z.txt"])
        self.assertEqual(gitutil.secret_looking(repo), [".env"])

    def test_a_modified_tracked_env_file_is_not_committed(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, ".env", "TOKEN=1\n")
        commit_file(repo, "b.txt")
        (repo / ".env").write_text("TOKEN=real-secret\n")   # the first entry of `status`
        (repo / "b.txt").write_text("work\n")
        gitutil.commit_all(repo, "chore(a): wip")
        self.assertEqual(git(repo, "show", "HEAD:.env"), "TOKEN=1")
        self.assertEqual(git(repo, "show", "HEAD:b.txt"), "work")
        self.assertEqual(gitutil.leftovers(repo), [".env"])  # it stays, and says so

    def test_git_output_keeps_leading_blanks(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        (repo / "a.txt").write_text("changed\n")
        self.assertEqual(gitutil.git(repo, "status", "--porcelain"), " M a.txt")


class OperationInProgressTest(unittest.TestCase):
    def conflicted_repo(self) -> Path:
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "f.txt", "base\n")
        git(repo, "checkout", "-q", "-b", "other")
        commit_file(repo, "f.txt", "other side\n", "other")
        git(repo, "checkout", "-q", "develop")
        commit_file(repo, "f.txt", "develop side\n", "develop")
        subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)    # conflicts
        return repo

    def test_a_merge_with_conflicts_is_never_closed_with_markers(self):
        repo = self.conflicted_repo()
        before = gitutil.head(repo)
        self.assertEqual(gitutil.operation_in_progress(repo), "a merge")
        self.assertEqual(gitutil.unmerged_files(repo), ["f.txt"])
        with self.assertRaises(gitutil.UnsafeToCommit) as cm:
            gitutil.commit_all(repo, "chore(a): wip")
        self.assertIn("middle of a merge", str(cm.exception))
        self.assertIn("f.txt", str(cm.exception))
        self.assertEqual(gitutil.head(repo), before)
        self.assertTrue((repo / ".git" / "MERGE_HEAD").exists())
        self.assertIn("<<<<<<<", (repo / "f.txt").read_text())

    def test_unmerged_paths_alone_block_a_commit(self):
        repo = self.conflicted_repo()
        (repo / ".git" / "MERGE_HEAD").unlink()             # the merge state is gone, the conflict is not
        with self.assertRaises(gitutil.UnsafeToCommit):
            gitutil.commit_all(repo, "chore(a): wip")

    def test_a_rebase_in_progress_is_seen(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        (repo / ".git" / "rebase-merge").mkdir()
        self.assertEqual(gitutil.operation_in_progress(repo), "a rebase")
        (repo / "a.txt").write_text("changed\n")
        with self.assertRaises(gitutil.UnsafeToCommit):
            gitutil.commit_all(repo, "chore(a): wip")


class SubmoduleTest(unittest.TestCase):
    def repo_with_submodule(self) -> Path:
        root = Path(tempfile.mkdtemp())
        lib = make_repo(root / "lib")
        commit_file(lib, "l.txt", "lib\n")
        repo = make_repo(root / "web")
        commit_file(repo, "a.txt")
        git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        git(repo, "commit", "-q", "-m", "add submodule")
        return repo

    def test_a_dirty_submodule_is_not_a_failed_commit(self):
        repo = self.repo_with_submodule()
        self.assertEqual(gitutil.submodule_paths(repo), ["vendor/lib"])
        (repo / "vendor" / "lib" / "l.txt").write_text("edited in the submodule\n")
        self.assertFalse(gitutil.is_dirty(repo))                 # nothing the superproject can commit
        self.assertIsNone(gitutil.commit_all(repo, "chore(a): wip"))     # and no "nothing to commit" error
        self.assertEqual(gitutil.leftovers(repo), ["vendor/lib"])        # but it is reported

    def test_only_the_parts_that_can_be_committed_are_committed(self):
        repo = self.repo_with_submodule()
        (repo / "vendor" / "lib" / "l.txt").write_text("edited in the submodule\n")
        (repo / "a.txt").write_text("own work\n")
        self.assertTrue(gitutil.commit_all(repo, "chore(a): wip"))
        self.assertEqual(git(repo, "show", "HEAD:a.txt"), "own work")
        self.assertEqual(gitutil.leftovers(repo), ["vendor/lib"])


class SubmoduleInARunTest(RunnerFixture):
    def test_a_task_that_leaves_work_in_a_submodule_is_partial_and_the_queue_goes_on(self):
        lib = make_repo(Path(tempfile.mkdtemp()) / "lib")
        commit_file(lib, "l.txt", "lib\n")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        git(self.repo, "commit", "-q", "-m", "add submodule")
        write_task(self, "a", branch="feat/x")
        write_task(self, "b", branch="feat/y")
        self.scenario_is(claude=[{"commit": "a.txt", "dirty": "vendor/lib/new.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        a = self.state.task("a")
        self.assertEqual(a.status, "partial")                    # not done: part of the work is not in git
        self.assertIn("could not be committed", a.note)
        self.assertIn("vendor/lib", a.note)
        self.assertEqual(self.state.task("b").status, "done")    # the repository was not left blocked


class PreflightNoticesTest(RunnerFixture):
    def test_plan_warns_about_submodules_and_about_a_repo_that_is_a_folder_of_another(self):
        from baton.config import load_config
        from baton.preflight import preflight
        lib = make_repo(Path(tempfile.mkdtemp()) / "lib")
        commit_file(lib, "l.txt", "lib\n")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        git(self.repo, "commit", "-q", "-m", "add submodule")
        write_task(self, "a", branch="feat/x")
        _errors, warnings, _tasks = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("has submodules" in w for w in warnings), warnings)
        # the same repository addressed as a folder inside a bigger one
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo/vendor/../src"\nwork_type = "feature"\nbranch = "feat/x"\n+++\nBuild a.\n')
        (self.repo / "src").mkdir()
        (self.repo / "src" / "m.py").write_text("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "src")
        _errors, warnings, _tasks = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("is a folder inside the repository" in w for w in warnings), warnings)


class NonUtf8NameTest(unittest.TestCase):
    def test_a_file_name_that_is_not_utf8_is_handled(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        bad = os.path.join(os.fsencode(str(repo)), b"bad\xff.txt")
        with open(bad, "wb") as f:
            f.write(b"x\n")
        files = gitutil.changed_files(repo)
        self.assertEqual(len(files), 1)
        self.assertEqual(gitutil.printable(files[0]), "bad\\udcff.txt")
        self.assertTrue(gitutil.commit_all(repo, "chore(a): wip"))
        self.assertFalse(gitutil.is_dirty(repo))
        self.assertEqual(len(tree_files(repo)), 2)

    def test_a_refused_secret_with_a_bad_name_is_excluded(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        (repo / "a.txt").write_text("changed\n")
        bad = os.path.join(os.fsencode(str(repo)), b"k\xff.pem")
        with open(bad, "wb") as f:
            f.write(b"x\n")
        self.assertTrue(gitutil.commit_all(repo, "chore(a): wip"))
        self.assertFalse(gitutil.is_dirty(repo))
        json.dumps([gitutil.printable(p) for p in gitutil.secret_looking(repo)])    # fits in the state file

    def test_changed_paths_come_back_printable(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        start = gitutil.head(repo)
        commit_file(repo, "café/ü.txt", "x\n", "unicode names")
        self.assertEqual(gitutil.changed_paths(repo, start), ["café/ü.txt"])    # not "caf\303\251/..."


class TagNamedLikeABranchTest(unittest.TestCase):
    def test_a_tag_is_not_a_branch(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        git(repo, "tag", "feat/x")
        self.assertTrue(gitutil.ref_exists(repo, "feat/x"))
        self.assertFalse(gitutil.branch_exists(repo, "feat/x"))
        msg = gitutil.checkout(repo, "feat/x", "develop")
        self.assertIn("created", msg)
        self.assertEqual(gitutil.current_branch(repo), "feat/x")         # a branch, not a detached HEAD

    def test_a_branch_wins_over_a_tag_of_the_same_name(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        git(repo, "branch", "feat/x")
        commit_file(repo, "b.txt")
        git(repo, "tag", "feat/x")                                      # a different commit
        gitutil.checkout(repo, "feat/x", None)
        self.assertEqual(gitutil.current_branch(repo), "feat/x")
        self.assertEqual(sorted(tree_files(repo)), ["a.txt"])           # the branch's commit

    def test_a_commit_hash_is_not_a_branch(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        self.assertFalse(gitutil.branch_exists(repo, gitutil.head(repo)))


def fake_git(directory: Path, body: str) -> dict:
    """A `git` that runs `body` instead of git, first on PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "git"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


class NeverReturnsTest(unittest.TestCase):
    def test_a_command_that_never_returns_is_cut_off(self):
        env = fake_git(Path(tempfile.mkdtemp()), "sleep 60\n")
        repo = make_repo(Path(tempfile.mkdtemp()))
        with mock.patch.dict(os.environ, env), mock.patch.object(gitutil, "GIT_TIMEOUT_S", 0.5):
            t0 = time.time()
            with self.assertRaises(gitutil.GitError) as cm:
                gitutil.git(repo, "status")
        self.assertLess(time.time() - t0, 10)
        self.assertIn("did not finish", str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))

    def test_git_gets_no_stdin_and_no_credential_prompt(self):
        out = Path(tempfile.mkdtemp()) / "seen"
        env = fake_git(Path(tempfile.mkdtemp()),
                       f'echo "prompt=$GIT_TERMINAL_PROMPT stdin=$(readlink /proc/self/fd/0)" > {out}\nexit 0\n')
        repo = make_repo(Path(tempfile.mkdtemp()))
        with mock.patch.dict(os.environ, env):
            gitutil.git(repo, "status", check=False)
        self.assertEqual(out.read_text().strip(), "prompt=0 stdin=/dev/null")

    def test_a_hook_that_hangs_does_not_hang_the_commit(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        commit_file(repo, "a.txt")
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nsleep 60\n")
        hook.chmod(0o755)
        (repo / "a.txt").write_text("changed\n")
        t0 = time.time()
        with mock.patch.object(gitutil, "COMMIT_TIMEOUT_S", 1.0), self.assertRaises(gitutil.GitError) as cm:
            gitutil.commit_all(repo, "chore(a): wip: real", verify=True)
        self.assertLess(time.time() - t0, 15)
        self.assertIn("did not finish", str(cm.exception))

    def test_git_missing_is_a_git_error(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with self.assertRaises(gitutil.GitError) as cm:
                gitutil.git(repo, "status")
        self.assertIn("not installed", str(cm.exception))


class ErrorMessagesTest(unittest.TestCase):
    def test_a_multi_line_error_is_one_line(self):
        repo = make_repo(Path(tempfile.mkdtemp()))
        fake = mock.Mock(returncode=1, stdout="", stderr="fatal: a\n\n  b\tc\nhint: d\n")
        with mock.patch.object(gitutil, "_run", return_value=fake), self.assertRaises(gitutil.GitError) as cm:
            gitutil.git(repo, "checkout", "x")
        self.assertNotIn("\n", str(cm.exception))
        self.assertIn("fatal: a b c hint: d", str(cm.exception))

    def test_a_long_error_is_cut(self):
        self.assertLessEqual(len(gitutil._one_line("x " * 2000)), 600)


class RecoverWithAMergeTest(RunnerFixture):
    def test_recover_does_not_close_a_merge_in_progress(self):
        write_task(self, "a", branch="feat/x")
        from baton.config import load_config
        from baton.runner import Runner
        from baton.tasks import load_tasks
        from tests.test_runner import FakeClock
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        commit_file(self.repo, "f.txt", "mine\n")
        git(self.repo, "checkout", "-q", "-b", "other", "develop")
        commit_file(self.repo, "f.txt", "theirs\n", "other")
        git(self.repo, "checkout", "-q", "feat/x")
        subprocess.run(["git", "-C", str(self.repo), "merge", "other"], capture_output=True)
        cfg = load_config(self.run_dir / "baton.toml")
        tasks = load_tasks(cfg)
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        runner.state.task("a").status = "running"
        before = gitutil.head(self.repo)
        runner.recover(tasks)                                           # must not raise
        self.assertEqual(gitutil.head(self.repo), before)
        self.assertIn("middle of a merge", runner.commit_failed[str(tasks[0].repo)])
        self.assertIn("could not recover", (cfg.logs_dir / "run.log").read_text())


if __name__ == "__main__":
    unittest.main()
