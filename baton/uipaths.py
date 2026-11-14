"""Which tasks touch the UI, and whether they say what the user will see.

A task touches a UI path when its front matter lists one in `paths` or its
text mentions a file that matches `plan.ui_paths`. Such a task should carry
a `## User journey` or a `## Reuse` section: the first says what the user
does and sees, the second which existing page or flow to build on.
"""

from __future__ import annotations

import re

from .isolation import matches_any
from .tasks import Task, ancestors

TOKEN_RE = re.compile(r"[\w@.\-/\[\]()]+")


def mentioned_paths(task: Task) -> list[str]:
    """Path-like words in the task text (inside backticks or not), plus `paths`."""
    found = list(task.paths)
    for tok in TOKEN_RE.findall(task.body):
        tok = tok.strip(".,;:()")
        if ("/" in tok or "." in tok) and tok not in found:
            found.append(tok.removeprefix("./"))
    return found


def ui_paths_touched(task: Task, patterns: list[str]) -> list[str]:
    return [p for p in mentioned_paths(task) if matches_any(p, patterns)]


def has_journey_or_reuse(task: Task) -> bool:
    return bool(task.section("User journey") or task.section("Reuse"))


def is_frontend_task(task: Task, patterns: list[str]) -> bool:
    """A task an agent implements that touches the UI."""
    return task.kind == "implement" and bool(ui_paths_touched(task, patterns))


def uncovered(tasks: list[Task], patterns: list[str]) -> list[Task]:
    """UI tasks that no browser-smoke verifier follows."""
    by_id = {t.id: t for t in tasks}
    checked = {a.id for s in tasks if s.template == "browser-smoke" for a in ancestors(s, by_id)}
    return [t for t in tasks if is_frontend_task(t, patterns) and t.id not in checked]


def warnings_for(tasks: list[Task], patterns: list[str], deferred: str | None = None) -> list[str]:
    """One warning per UI task without a journey or reuse section, and one per
    UI task that no browser-smoke verifier follows, or, when browser-smoke is
    deliberately deferred, one line for all of them."""
    out: list[str] = []
    by_id = {t.id: t for t in tasks}
    checked = {a.id for s in tasks if s.template == "browser-smoke" for a in ancestors(s, by_id)}
    for t in tasks:
        if not is_frontend_task(t, patterns):
            continue
        touched = ui_paths_touched(t, patterns)
        if not has_journey_or_reuse(t):
            out.append(f"task {t.id} touches UI paths ({', '.join(touched[:3])}"
                       f"{', ...' if len(touched) > 3 else ''}) but has neither a \"## User journey\" "
                       "nor a \"## Reuse\" section")
        if t.id not in checked and not deferred:
            out.append(f"task {t.id} touches UI paths but no browser-smoke verifier depends on it, "
                       "so nothing opens the page in a browser before its dependents run")
    skipped = uncovered(tasks, patterns)
    if deferred and skipped:
        out.append(f"browser-smoke is deferred ({deferred}); {len(skipped)} UI task(s) will not be opened in a "
                   f"browser: {', '.join(t.id for t in skipped)} (listed under \"Not verified\")")
    return out
