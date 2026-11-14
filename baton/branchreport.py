"""End-of-run branch report: which branches the run left in each repository, whether any two of
them conflict, and a merge order. baton never merges or pushes; this only reads (`merge-tree`
computes a merge in memory and touches neither the work tree nor any ref)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config
from .state import RunState
from .tasks import Task

MAX_BRANCHES = 25        # per repository: the pair checks grow with the square
TIMEOUT_S = 60


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout


def _exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] == 0


def ahead(repo: Path, base: str, branch: str) -> int | None:
    rc, out = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else None


def contains(repo: Path, outer: str, inner: str) -> bool:
    """True when every commit of `inner` is already in `outer`."""
    return _git(repo, "merge-base", "--is-ancestor", inner, outer)[0] == 0


def conflicts(repo: Path, a: str, b: str) -> list[str] | None:
    """The files a merge of `b` into `a` would conflict in; [] when it merges cleanly; None when
    this git cannot tell."""
    rc, out = _git(repo, "merge-tree", "--write-tree", "--name-only", a, b)
    if rc == 0:
        return []
    if rc == 1:
        lines = out.splitlines()
        files = []
        for ln in lines[1:]:          # line 0 is the resulting tree; the file names follow until a blank line
            if not ln.strip():
                break
            files.append(ln.strip())
        return files or ["(conflict)"]
    rc, base = _git(repo, "merge-base", a, b)       # git older than 2.38: the three-argument form
    if rc != 0 or not base.strip():
        return None
    rc, out = _git(repo, "merge-tree", base.strip(), a, b)
    if rc != 0:
        return None
    return ["(conflict)"] if "<<<<<<<" in out else []


def _branches_by_repo(ordered: list[Task], state: RunState) -> dict[Path, list[tuple[Task, str]]]:
    """The branches the run worked on, per repository, in the order the tasks ran (each branch once)."""
    by_repo: dict[Path, list[tuple[Task, str]]] = {}
    for t in ordered:
        ts = state.task(t.id)
        if not (t.runs_agent and t.branch) or not (ts.attempts or ts.commits):
            continue
        repo = t.repo_key and Path(t.repo_key) or t.repo
        if t.branch not in [b for _, b in by_repo.get(repo, [])]:
            by_repo.setdefault(repo, []).append((t, t.branch))
    return by_repo


def _branch_rows(cfg: Config, repo: Path, items: list[tuple[Task, str]]) -> list[str]:
    rows = []
    for t, b in items:
        base = t.base or cfg.default_base
        n = ahead(repo, base, b)
        rows.append(f"| `{b}` | {t.id} | {'?' if n is None else n} | `{base}` |")
    return rows


def _pair_note(repo: Path, a: str, b: str) -> str | None:
    """What to know about merging two branches of the run, or None when they merge cleanly."""
    if contains(repo, b, a):
        return f"- `{a}` is already inside `{b}`: merge `{b}` and `{a}` comes with it"
    if contains(repo, a, b):
        return f"- `{b}` is already inside `{a}`"
    files = conflicts(repo, a, b)
    if files is None:
        return f"- `{a}` and `{b}`: this git cannot check for conflicts"
    if files:
        shown = ", ".join(f"`{f}`" for f in files[:8]) + (" …" if len(files) > 8 else "")
        return (f"- **`{a}` and `{b}` conflict** in {shown}: merge `{a}` first, "
                f"then resolve `{b}` on top of it")
    return None


def _repo_section(cfg: Config, repo: Path, items: list[tuple[Task, str]]) -> list[str]:
    out = [f"## {repo.name}", ""]
    items = [(t, b) for t, b in items if _exists(repo, b)]
    if not items:
        return out + ["No branch of this run exists any more.", ""]
    out += ["| branch | task | commits ahead | of |", "|---|---|---|---|", *_branch_rows(cfg, repo, items), ""]
    names = [b for _, b in items][:MAX_BRANCHES]
    if len(items) > MAX_BRANCHES:
        out += [f"(only the first {MAX_BRANCHES} branches are compared)", ""]
    notes = [n for i, a in enumerate(names) for b in names[i + 1:] if (n := _pair_note(repo, a, b))]
    return out + ["### Conflicts between branches", "", *(notes or ["None found."]), "",
                  "### Suggested order", "", *[f"{i}. `{b}`" for i, b in enumerate(names, 1)], ""]


def report(cfg: Config, ordered: list[Task], state: RunState) -> str:
    """BRANCHES.md as text; '' when the run made no branch."""
    by_repo = _branches_by_repo(ordered, state)
    if not by_repo:
        return ""
    out = ["# Branches left by the run", "",
           "baton never merges or pushes. Branches are listed in the order the tasks ran, which is a "
           "safe order to merge them in; a conflict below tells you where to expect work.", ""]
    for repo, items in by_repo.items():
        out += _repo_section(cfg, repo, items)
    return "\n".join(out)
