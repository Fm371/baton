"""Branches that already exist, a detached HEAD, a branch an agent left a repository on, protected
branch names, and the clearer refusals of preflight (dirty repository, missing base, bare repository)."""

import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from baton import branches, gitutil
from baton.config import ConfigError, load_config
from baton.preflight import dirty_message, plan_text, preflight
from baton.runner import Runner
from baton.tasks import load_tasks
from tests.test_branches import write_task
from tests.test_runner import FakeClock, RunnerFixture, git


def make_branch(repo: Path, name: str, filename: str, start: str = "develop") -> None:
    """A branch with one extra commit, made from `start`, with the repository back on develop afterwards."""
    git(repo, "checkout", "-q", "-b", name, start)
    (repo / filename).write_text(f"{filename}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"old work on {name}")
    git(repo, "checkout", "-q", "develop")


class AssessTest(RunnerFixture):
    def test_a_missing_branch_is_not_reported(self):
        self.assertIsNone(branches.assess(self.repo, "feat/none", "develop", []))

    def test_counts_over_and_behind_the_base(self):
        make_branch(self.repo, "feat/a", "a.txt")
        (self.repo / "d.txt").write_text("d\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "develop moves on")
        rep = branches.assess(self.repo, "feat/a", "develop", [])
        self.assertEqual((rep.ahead, rep.behind), (1, 1))
        self.assertTrue(rep.ok)                       # behind the base alone is not a problem
        self.assertIn("1 commit over develop", rep.describe())

    def test_an_orphan_branch_has_no_common_history(self):
        git(self.repo, "checkout", "-q", "--orphan", "feat/o")
        git(self.repo, "rm", "-rf", "-q", ".")
        (self.repo / "o.txt").write_text("o\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "orphan")
        git(self.repo, "checkout", "-q", "develop")
        rep = branches.assess(self.repo, "feat/o", "develop", [])
        self.assertTrue(rep.unrelated)
        self.assertFalse(rep.ok)
        self.assertIn("no common history", rep.describe())

    def test_a_branch_without_its_dependency_is_a_problem_that_a_rebase_can_fix(self):
        make_branch(self.repo, "feat/schema", "schema.txt")
        make_branch(self.repo, "feat/endpoints", "e.txt")
        rep = branches.assess(self.repo, "feat/endpoints", "feat/schema", ["feat/schema"])
        self.assertEqual(rep.missing, [("feat/schema", 1)])
        self.assertEqual(rep.rebase_target, "feat/schema")
        self.assertIn("does not contain feat/schema", rep.problems[0])

    def test_a_branch_that_contains_its_dependency_is_fine(self):
        make_branch(self.repo, "feat/schema", "schema.txt")
        make_branch(self.repo, "feat/endpoints", "e.txt", start="feat/schema")
        self.assertTrue(branches.assess(self.repo, "feat/endpoints", "feat/schema", ["feat/schema"]).ok)

    def test_a_tag_of_the_same_name_is_not_a_branch(self):
        git(self.repo, "tag", "feat/t")
        self.assertIsNone(branches.assess(self.repo, "feat/t", "develop", []))


class ExistingBranchRunTest(RunnerFixture):
    def two_tasks(self):
        make_branch(self.repo, "feat/schema", "old-schema.txt")
        make_branch(self.repo, "feat/endpoints", "old-endpoints.txt")      # made before the schema work existed
        write_task(self, "a", branch="feat/schema")
        write_task(self, "b", branch="feat/endpoints", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])

    def test_plan_shows_the_old_branch_and_what_the_run_will_do(self):
        self.two_tasks()
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        note = next(w for w in warnings if w.startswith("task b:"))
        self.assertIn("does not contain feat/schema", note)
        self.assertIn("the run will refuse this task", note)
        self.assertIn("existing_branch", note)
        self.assertIn("[branch exists: 1 commit over feat/schema", plan_text(cfg, tasks))

    def test_by_default_the_dependent_is_refused_instead_of_built_without_its_dependency(self):
        self.two_tasks()
        self.run_baton()
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertEqual(self.state.tasks["b"].status, "failed")
        self.assertIn("does not contain feat/schema", self.state.tasks["b"].note)
        self.assertEqual(len(self.calls()), 1)               # no agent ran for b
        self.assertEqual(git(self.repo, "rev-list", "--count", "develop..feat/endpoints"), "1")   # untouched

    def test_warn_goes_on_and_says_so(self):
        self.two_tasks()
        self.write_config(existing_branch="warn")
        self.run_baton()
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertIn("WARNING feat/endpoints does not contain feat/schema", self.log)

    def test_rebase_moves_the_branch_onto_the_dependency_first(self):
        self.two_tasks()
        self.write_config(existing_branch="rebase")
        self.run_baton()
        self.assertEqual(self.state.tasks["b"].status, "done")
        self.assertIn("rebased feat/endpoints onto feat/schema", self.log)
        self.assertTrue(gitutil.is_ancestor(self.repo, "feat/schema", "feat/endpoints"))
        self.assertEqual(git(self.repo, "show", "feat/endpoints:old-endpoints.txt"), "old-endpoints.txt")
        self.assertIn("a.txt", git(self.repo, "ls-tree", "--name-only", "feat/endpoints"))      # the dependency's new work

    def test_a_rebase_that_conflicts_is_aborted_and_the_task_refused(self):
        make_branch(self.repo, "feat/schema", "same.txt")
        git(self.repo, "checkout", "-q", "-b", "feat/endpoints", "develop")
        (self.repo / "same.txt").write_text("different\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "conflicting")
        git(self.repo, "checkout", "-q", "develop")
        write_task(self, "a", branch="feat/schema")
        write_task(self, "b", branch="feat/endpoints", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.write_config(existing_branch="rebase")
        self.run_baton()
        self.assertEqual(self.state.tasks["b"].status, "failed")
        self.assertIn("rebase of feat/endpoints onto feat/schema", self.state.tasks["b"].note)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")        # no rebase left half done

    def test_an_orphan_branch_is_never_rebased(self):
        git(self.repo, "checkout", "-q", "--orphan", "feat/o")
        git(self.repo, "rm", "-rf", "-q", ".")
        (self.repo / "o.txt").write_text("o\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "orphan")
        git(self.repo, "checkout", "-q", "develop")
        write_task(self, "a", branch="feat/first")          # runs first, so the run leaves metrics behind
        write_task(self, "b", branch="feat/o")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.write_config(existing_branch="rebase")
        self.run_baton()
        self.assertEqual(self.state.tasks["b"].status, "failed")
        self.assertIn("shares no history with develop", self.state.tasks["b"].note)

    def test_a_task_that_continues_its_own_branch_is_not_checked_again(self):
        make_branch(self.repo, "feat/schema", "schema.txt")
        make_branch(self.repo, "feat/endpoints", "e.txt")
        write_task(self, "a", branch="feat/schema")
        write_task(self, "b", branch="feat/endpoints", deps=["a"])
        cfg = load_config(self.run_dir / "baton.toml")
        runner = Runner(cfg, clock=FakeClock(), echo=False)
        runner.ordered = load_tasks(cfg)
        b = next(t for t in runner.ordered if t.id == "b")
        git(self.repo, "checkout", "-q", "feat/endpoints")
        runner.check_existing_branch(b)                       # already on it: nothing to say
        self.assertEqual(runner.state.task("b").status, "pending")

    def test_the_setting_is_checked(self):
        self.write_config(existing_branch="sometimes")
        with self.assertRaises(ConfigError) as e:
            load_config(self.run_dir / "baton.toml")
        self.assertIn("existing_branch", str(e.exception))
        self.write_config()
        self.assertEqual(load_config(self.run_dir / "baton.toml").existing_branch, "fail")


class DetachedAndStrayTest(RunnerFixture):
    def test_a_detached_head_is_put_back_by_its_commit(self):
        start = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--detach")
        write_task(self, "a", branch="feat/x")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        self.assertIn("the detached HEAD at " + start[:12], plan_text(cfg, tasks))
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), start)
        self.assertIn("back on the detached HEAD at " + start[:12], self.log)
        self.assertEqual(self.state.initial_branches, {})
        self.assertEqual(git(self.repo, "rev-list", "--count", "feat/x"), "2")      # the task's work is on its branch

    def test_a_branch_an_agent_left_behind_is_never_remembered_as_the_users(self):
        cfg = load_config(self.run_dir / "baton.toml")
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.ordered = []
        git(self.repo, "checkout", "-q", "-b", "rogue")
        r.note_stray_branch(self.repo, "rogue")
        r.note_initial_branch(self.repo)
        self.assertEqual(r.state.initial_branches, {})
        self.assertIn("not remembered as yours", (cfg.logs_dir / "run.log").read_text())

    def test_the_users_branch_comes_back_from_a_stray_one(self):
        cfg = load_config(self.run_dir / "baton.toml")
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.ordered = []
        r.note_initial_branch(self.repo)                       # develop
        git(self.repo, "checkout", "-q", "-b", "rogue")
        r.note_stray_branch(self.repo, "rogue")
        r.restore_branches()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual((r.state.initial_branches, r.state.stray_branches), ({}, {}))

    def test_a_stray_branch_keeps_the_record_until_the_user_branch_is_back(self):
        cfg = load_config(self.run_dir / "baton.toml")
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.ordered = []
        r.note_initial_branch(self.repo)
        git(self.repo, "checkout", "-q", "-b", "rogue")
        (self.repo / "dirty.txt").write_text("x\n")
        r.note_stray_branch(self.repo, "rogue")
        r.restore_branches()                                   # dirty: stays on rogue, still remembers develop
        self.assertEqual(git(self.repo, "branch", "--show-current"), "rogue")
        self.assertEqual(r.state.initial_branches[str(self.repo)], "develop")
        self.assertIn("left on rogue", (cfg.logs_dir / "run.log").read_text())

    def test_a_branch_the_user_moved_to_is_left_alone_and_the_log_says_so(self):
        cfg = load_config(self.run_dir / "baton.toml")
        r = Runner(cfg, clock=FakeClock(), echo=False)
        r.ordered = []
        r.note_initial_branch(self.repo)
        git(self.repo, "checkout", "-q", "-b", "users-own")
        r.restore_branches()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "users-own")
        self.assertIn("which baton did not choose", (cfg.logs_dir / "run.log").read_text())


class PreflightMessagesTest(RunnerFixture):
    def test_a_dirty_repository_names_its_files(self):
        write_task(self, "a", branch="feat/x")
        (self.repo / "scratch.txt").write_text("x\n")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        text = "\n".join(errors)
        self.assertIn("has uncommitted changes; commit them first", text)
        self.assertIn("?? scratch.txt", text)

    def test_baton_folders_inside_the_repository_are_named_as_the_cause(self):
        inside = self.repo / "task-auto" / "results"
        inside.mkdir(parents=True)
        (inside / "x.md").write_text("x\n")
        cfg = types.SimpleNamespace(base_dir=self.repo / "task-auto")
        msg = dirty_message(cfg, self.repo)
        self.assertIn("?? task-auto/results/x.md", msg)
        self.assertIn("baton's own folders", msg)
        self.assertIn("task-auto/results/", msg)

    def test_a_long_list_of_changes_is_cut(self):
        for i in range(9):
            (self.repo / f"f{i}.txt").write_text("x\n")
        self.assertIn("… and 4 more", dirty_message(types.SimpleNamespace(base_dir=self.root), self.repo))

    def test_a_missing_base_with_a_remote_branch_suggests_the_command(self):
        git(self.repo, "update-ref", "refs/remotes/origin/feat-base", "HEAD")
        write_task(self, "a", branch="feat/x", base="feat-base")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        text = "\n".join(errors)
        self.assertIn("base feat-base does not exist", text)
        self.assertIn(f"git -C {self.repo} branch feat-base origin/feat-base", text)
        self.assertIn("never fetches", text)

    def test_a_missing_base_without_a_remote_branch_says_to_fetch_yourself(self):
        write_task(self, "a", branch="feat/x", base="nowhere")
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        text = "\n".join(errors)
        self.assertIn("base nowhere does not exist", text)
        self.assertIn("baton never fetches", text)

    def test_a_bare_repository_is_named_as_such(self):
        bare = Path(tempfile.mkdtemp()) / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        (self.run_dir / "tasks" / "a.md").write_text(
            f'+++\nrepo = "{bare}"\nwork_type = "feature"\nbranch = "feat/x"\n+++\nBuild a.\n')
        errors, _, _ = preflight(load_config(self.run_dir / "baton.toml"))
        text = "\n".join(errors)
        self.assertIn("is a bare repository", text)
        self.assertNotIn("is not a git repository", text)


class ProtectedBranchTest(RunnerFixture):
    def warnings(self):
        return preflight(load_config(self.run_dir / "baton.toml"))[1]

    def test_a_task_on_the_default_base_or_main_is_warned_about(self):
        write_task(self, "a", branch="develop")
        write_task(self, "b", branch="main")
        text = "\n".join(self.warnings())
        self.assertIn("task a: its branch 'develop' is a main line (the default base)", text)
        self.assertIn("task b: its branch 'main' is a main line (main/master)", text)

    def test_a_task_on_the_branch_checked_out_now_is_warned_about(self):
        git(self.repo, "checkout", "-q", "-b", "my-work")
        write_task(self, "a", branch="my-work")
        self.assertIn("commits onto your own working branch", "\n".join(self.warnings()))

    def test_an_ordinary_task_branch_is_not(self):
        write_task(self, "a", branch="feat/x")
        self.assertEqual([w for w in self.warnings() if "main line" in w or "working branch" in w], [])

    def test_a_task_that_was_running_may_be_on_its_own_branch(self):
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        write_task(self, "a", branch="feat/x")
        cfg = load_config(self.run_dir / "baton.toml")
        task = load_tasks(cfg)[0]
        from baton.preflight import protected_branch_warnings
        self.assertEqual(protected_branch_warnings(cfg, task, True), [])


if __name__ == "__main__":
    unittest.main()
