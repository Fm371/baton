"""What was not really verified, for the "Not verified" section of SUMMARY.md.

QA starts here. Three sources:

  * the `## Not verified` section an agent writes in its result file: what it
    checked only with mocks or fixtures, and what it could not run;
  * the `## Not checked` section of a built-in check's report (a browser-smoke
    route it could not open, an API host it was not told about);
  * the plan: a UI task that no browser-smoke follows, or the reason browser-smoke
    is deferred (`[browser_smoke] deferred = "..."`).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import uipaths
from .config import Config
from .state import FINISHED, RunState
from .tasks import Task, markdown_section

NOTHING = re.compile(r"^(none|nothing|n/?a|-)\.?$", re.IGNORECASE)


BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
BULLET_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def section_items(text: str | None) -> list[str]:
    """The entries of a section: its bullets, or its whole text when it has none. A bullet
    wrapped over several lines is one entry (the continuation lines are joined to it); a blank
    line or the next bullet ends it."""
    if not text:
        return []
    bullets: list[list[str]] = []
    open_bullet: list[str] | None = None
    for ln in text.splitlines():
        if BULLET_RE.match(ln):
            open_bullet = [BULLET_MARK_RE.sub("", ln).strip()]
            bullets.append(open_bullet)
        elif not ln.strip():
            open_bullet = None
        elif open_bullet is not None:
            open_bullet.append(ln.strip())
    items = [" ".join(b) for b in bullets] or [" ".join(text.split())]
    return [i for i in items if i and not NOTHING.match(i)]


def reported(results_dir: Path, task: Task) -> list[str]:
    """What the task's own result file says it did not verify."""
    try:
        text = (results_dir / f"{task.id}.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return section_items(markdown_section(text, "Not checked" if task.template else "Not verified"))


def needs_from_you(results_dir: Path, task: Task) -> list[str]:
    """What the task's own result file says a person has to do or provide (`## Needs from you`)."""
    try:
        text = (results_dir / f"{task.id}.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return section_items(markdown_section(text, "Needs from you"))


def warnings_markdown(cfg: Config, tasks: list[Task], state: RunState) -> str:
    """The `## Warnings` block at the top of SUMMARY.md: secret-looking files that WIP commits took."""
    lines = []
    for t in tasks:
        files = state.task(t.id).suspect_files
        if files:
            lines.append(f"- **{t.id}**: committed secret-looking files ({', '.join(files[:8])}"
                         f"{' …' if len(files) > 8 else ''}); look at them before anything is pushed "
                         "(`[run] wip_warn_patterns`)")
    if not lines:
        return ""
    return "## Warnings\n\n" + "\n".join(lines) + "\n\n"


def needs_markdown(cfg: Config, tasks: list[Task], state: RunState) -> str:
    """The `## Needs from you` block for the top of SUMMARY.md: keys, env vars, migrations and
    manual steps the tasks reported, gathered in one place. Empty when no task asked for anything."""
    lines: list[str] = []
    for t in tasks:
        if state.task(t.id).status not in FINISHED and state.task(t.id).status != "blocked":
            continue
        items = needs_from_you(cfg.results_dir, t)
        if items:
            lines.append(f"- **{t.id}**: {items[0]}")
            lines += [f"  - {i}" for i in items[1:]]
    if not lines:
        return ""
    return "## Needs from you\n\n" + "\n".join(lines) + "\n\n"


def deferred_reason(cfg: Config) -> str | None:
    reason = cfg.browser_smoke.get("deferred")
    return str(reason) if reason else None


def collect(cfg: Config, tasks: list[Task], state: RunState) -> list[tuple[str, list[str]]]:
    """(who, what) pairs, plan-level entries first."""
    out: list[tuple[str, list[str]]] = []
    ui = uipaths.uncovered(tasks, cfg.ui_paths)
    reason = deferred_reason(cfg)
    if reason and ui:
        out.append(("plan", [f"browser-smoke is deferred ({reason}): no page was opened in a browser for "
                             + ", ".join(t.id for t in ui)]))
    for t in tasks:
        ts = state.task(t.id)
        items = reported(cfg.results_dir, t) if ts.status in FINISHED or ts.status == "blocked" else []
        if not reason and ts.status in FINISHED and t in ui:
            items.append("UI task that no browser-smoke follows: nothing opened the page in a browser")
        if items:
            out.append((t.id, items))
    return out


def markdown(entries: list[tuple[str, list[str]]]) -> str:
    lines = ["## Not verified", "", "QA starts here: these were checked only with mocks or fixtures, or not at all.", ""]
    if not entries:
        return "\n".join(lines + ["Nothing declared."]) + "\n"
    for who, items in entries:
        lines.append(f"- **{who}**: {items[0]}")
        lines += [f"  - {i}" for i in items[1:]]
    return "\n".join(lines) + "\n"
