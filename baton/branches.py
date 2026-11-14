"""What to do when a task's branch already exists.

baton creates a task's branch from its base when it is missing. When it is there already (left by an
earlier night, by hand, or by an earlier run of this task) it is used as it is, and that is only right
when it holds the work the task builds on. This module looks at such a branch and says what is wrong
with it; the runner applies `[run] existing_branch` (warn, rebase or fail) and `baton plan` prints it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil
from .tasks import Task, effective_deps

MODES = ("warn", "rebase", "fail")


@dataclass
class BranchReport:
    branch: str
    base: str | None
    ahead: int | None = None            # commits on the branch that the base lacks
    behind: int | None = None           # commits on the base that the branch lacks
    unrelated: bool = False             # no history in common with the base (an orphan branch)
    # (dependency branch, commits of it that the branch lacks)
    missing: list[tuple[str, int]] = field(default_factory=list)
    rebase_target: str | None = None    # where a rebase would put it; None when no rebase can fix it

    @property
    def problems(self) -> list[str]:
        out = []
        if self.unrelated:
            out.append(f"{self.branch} shares no history with {self.base}")
        for dep, n in self.missing:
            out.append(f"{self.branch} does not contain {dep} ({n} commit{'s' if n != 1 else ''} of it "
                       "missing), so the task would not see the work it depends on")
        return out

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        """A few words for `baton plan` and the log."""
        parts = []
        if self.ahead is not None and self.base:
            parts.append(f"{self.ahead} commit{'s' if self.ahead != 1 else ''} over {self.base}")
        if self.behind:
            parts.append(f"{self.behind} behind it")
        if self.unrelated:
            parts.append("no common history")
        return ", ".join(parts) or "same as its base"


def dependency_branches(task: Task, by_id: dict[str, Task]) -> list[str]:
    """Branches of the tasks this one depends on, in its own repository, other than its own branch.
    A verifier's own branch is not something another task builds on."""
    out: list[str] = []
    for dep in effective_deps(task, by_id):
        if dep.repo != task.repo or not dep.runs_agent or not dep.branch:
            continue
        if dep.is_verifier or dep.branch == task.branch or dep.branch in out:
            continue
        out.append(dep.branch)
    return out


def assess(repo: Path, branch: str, base: str | None, dep_branches: list[str]) -> BranchReport | None:
    """None when the branch does not exist (it will be created from the base)."""
    if not gitutil.has_branch(repo, branch):
        return None
    rep = BranchReport(branch=branch, base=base)
    if base and (gitutil.has_branch(repo, base) or base == "HEAD"):
        if not gitutil.share_history(repo, branch, base):
            rep.unrelated = True
        else:
            ab = gitutil.ahead_behind(repo, branch, base)
            if ab:
                rep.ahead, rep.behind = ab
    for dep in dep_branches:
        if not gitutil.has_branch(repo, dep):
            continue                                    # not made yet: nothing of it can be missing
        if not gitutil.is_ancestor(repo, dep, branch):
            ab = gitutil.ahead_behind(repo, branch, dep)
            rep.missing.append((dep, ab[1] if ab else 0))
    if rep.missing and not rep.unrelated and len(rep.missing) == 1:
        rep.rebase_target = rep.missing[0][0]           # one dependency: the branch can be moved onto it
    return rep


def fix_hint(rep: BranchReport) -> str:
    if rep.unrelated:
        return (f"rename or delete the old branch {rep.branch} (`git branch -m {rep.branch} {rep.branch}-old`), "
                "or point the task at another `branch`/`base`")
    return ("rebase it yourself (`git rebase <dependency branch>` while on it), delete the old branch, "
            "or set [run] existing_branch = \"rebase\" to let baton rebase it")


def plan_note(rep: BranchReport, mode: str) -> str:
    """The warning `baton plan` prints for a branch with problems."""
    if mode == "warn":
        consequence = "the run will use it anyway"
    elif mode == "rebase" and rep.rebase_target:
        consequence = f"the run will rebase it onto {rep.rebase_target}"
    elif mode == "rebase":
        consequence = "baton cannot rebase it, so the run will refuse this task"
    else:
        consequence = "the run will refuse this task"
    return f"{'; '.join(rep.problems)} ({consequence}; {fix_hint(rep)})"
