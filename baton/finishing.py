"""How a task's final status and note are decided once its attempts are over.

`decide_finish` is a pure function of what is known at the end of a task; `Runner.finish` gathers
those facts (commits, work that could not be committed, files refused or flagged, files a verifier
touched) and records the decision. Keeping the rules here makes the decision table testable
without a repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FinishFacts:
    """What the end of a task knows. `left` is why some work could not be committed (None: all of it was)."""
    commits: int = 0
    runs_agent: bool = True
    is_verifier: bool = False
    left: str | None = None
    refused: list[str] = field(default_factory=list)          # files kept out of the commit as secrets
    suspect: list[str] = field(default_factory=list)          # secret-looking files that were committed
    stray: list[str] = field(default_factory=list)            # non-test files a verifier changed


def _join(note: str, extra: str) -> str:
    return (note + "; " if note else "") + extra


def _listed(files: list[str], limit: int = 5) -> str:
    return ", ".join(files[:limit]) + (" …" if len(files) > limit else "")


def decide_finish(status: str, note: str, facts: FinishFacts) -> tuple[str, str]:
    """The final (status, note) for a task that ended as `status` with `note`.

    Work that is not in git is not done; a partial result with no commits is a failure; a verifier
    that changed anything but tests is blocked."""
    if facts.left:
        note = _join(note, f"not all work could be committed: {facts.left}")
        if status == "done":
            status = "partial"
    if facts.refused:
        note = _join(note, f"{len(facts.refused)} file(s) not committed because they look like secrets: "
                           f"{_listed(facts.refused)}")
    if facts.suspect:
        note = _join(note, f"committed secret-looking files: {_listed(facts.suspect)} (check before pushing)")
    if status == "partial" and facts.commits == 0:
        status, note = "failed", _join(note, "partial with no commits")
    if status == "done" and facts.commits == 0 and facts.runs_agent:
        note = _join(note, "no commits")
    if facts.is_verifier and facts.stray:
        status, note = "blocked", _join(note, "verifier touched non-test files: " + ", ".join(facts.stray))
    return status, note
