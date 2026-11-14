"""BRANCHES.md text for real branches: containment, a conflict, a missing branch, no branch at all
(pins the report so it can be restructured)."""

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

from baton import branchreport


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", f"{name}: {text}")


class State:
    def __init__(self, **tasks):
        self.t = {k: NS(attempts=v[0], commits=v[1]) for k, v in tasks.items()}

    def task(self, tid):
        return self.t.get(tid, NS(attempts=0, commits=0))


def task(tid, branch, repo, base=None, agent=True):
    return NS(id=tid, branch=branch, repo=repo, repo_key=str(repo), base=base, runs_agent=agent)


class BranchReportTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()) / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "develop")
        commit(self.repo, "f.txt", "base\n")
        git(self.repo, "checkout", "-q", "-b", "feat/one")
        commit(self.repo, "f.txt", "one\n")
        git(self.repo, "checkout", "-q", "-b", "feat/one-more")
        commit(self.repo, "g.txt", "more\n")
        git(self.repo, "checkout", "-q", "develop")
        git(self.repo, "checkout", "-q", "-b", "feat/two")
        commit(self.repo, "f.txt", "two\n")
        git(self.repo, "checkout", "-q", "develop")
        self.cfg = NS(default_base="develop")

    def test_the_whole_report(self):
        ordered = [task("a", "feat/one", self.repo), task("b", "feat/one-more", self.repo),
                   task("c", "feat/two", self.repo), task("d", "feat/gone", self.repo),
                   task("e", "feat/skipped", self.repo), task("v", None, self.repo, agent=True),
                   task("n", "feat/noagent", self.repo, agent=False)]
        state = State(a=(1, 1), b=(1, 1), c=(2, 1), d=(1, 0), v=(1, 0), n=(1, 1))
        text = branchreport.report(self.cfg, ordered, state)
        self.assertEqual(text, "\n".join([
            "# Branches left by the run",
            "",
            "baton never merges or pushes. Branches are listed in the order the tasks ran, which is a safe order "
            "to merge them in; a conflict below tells you where to expect work.",
            "",
            "## proj",
            "",
            "| branch | task | commits ahead | of |",
            "|---|---|---|---|",
            "| `feat/one` | a | 1 | `develop` |",
            "| `feat/one-more` | b | 2 | `develop` |",
            "| `feat/two` | c | 1 | `develop` |",
            "",
            "### Conflicts between branches",
            "",
            "- `feat/one` is already inside `feat/one-more`: merge `feat/one-more` and `feat/one` comes with it",
            "- **`feat/one` and `feat/two` conflict** in `f.txt`: merge `feat/one` first, then resolve `feat/two` on top of it",
            "- **`feat/one-more` and `feat/two` conflict** in `f.txt`: merge `feat/one-more` first, then resolve "
            "`feat/two` on top of it",
            "",
            "### Suggested order",
            "",
            "1. `feat/one`",
            "2. `feat/one-more`",
            "3. `feat/two`",
            "",
        ]))

    def test_empty_when_nothing_ran_and_a_note_when_no_branch_exists_any_more(self):
        self.assertEqual(branchreport.report(self.cfg, [task("a", "feat/one", self.repo)], State()), "")
        text = branchreport.report(self.cfg, [task("d", "feat/gone", self.repo)], State(d=(1, 0)))
        self.assertIn("No branch of this run exists any more.", text)

    def test_only_the_first_branches_are_compared(self):
        with unittest.mock.patch.object(branchreport, "MAX_BRANCHES", 2):
            ordered = [task("a", "feat/one", self.repo), task("b", "feat/one-more", self.repo),
                       task("c", "feat/two", self.repo)]
            text = branchreport.report(self.cfg, ordered, State(a=(1, 1), b=(1, 1), c=(1, 1)))
        self.assertIn("(only the first 2 branches are compared)", text)
        self.assertNotIn("feat/two` conflict", text)


if __name__ == "__main__":
    unittest.main()
