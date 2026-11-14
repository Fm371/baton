"""Environment variables a task needs, and tests that skipped because one was missing.

Why: a verifier that starts from a new shell without its database URL does not fail; its
database tests *skip*, and the run ends minutes later with a `blocked` or, worse, a green
result that checked nothing. `required_env = [...]` (in [run], in a provider table, or in a task
file) is checked by `baton plan` and again before a task starts, so a missing variable stops the
run with a clear message before an attempt is spent. A result that still reports tests skipped for
a missing variable is classified `blocked:env` (`TaskState.reason = "env"`), never done.
"""

from __future__ import annotations

import os
import re

from .config import Config
from .state import FINISHED, load_state
from .tasks import Task

# a line that talks about a skip and names an unset variable (APP_TEST_DB_URI not set), or
# says an environment variable is missing
_NAME = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
_UNSET = r"(?i:(?:is\s+)?(?:not\s+set|unset|missing|undefined|empty|not\s+defined))"
_NAMED = re.compile(rf"\b({_NAME})\b[^\n]{{0,40}}?\b{_UNSET}\b|\b{_UNSET}\b[^\n]{{0,20}}?\b({_NAME})\b")
_GENERIC = re.compile(r"\benv(?:ironment)?(?:\s+var(?:iable)?s?)?\b[^\n]{0,60}?\b" + _UNSET + r"\b", re.I)


def skipped_for_env(text: str) -> str | None:
    """The first line of `text` that says tests were skipped because an environment variable is
    missing, shortened; None when there is none. Only lines that mention a skip count, so a
    variable named in prose does not turn a result into a block."""
    for line in text.splitlines():
        if not re.search(r"\bskip", line, re.I):
            continue
        m = _NAMED.search(line)
        var = None
        if m:
            var = next((g for g in m.groups() if g and "_" in g), None)
        if var or _GENERIC.search(line):
            return f"{var} is not set" if var else line.strip()[:160]
    return None


def _is_set(name: str, provider_env: dict[str, str] | None) -> bool:
    return bool(os.environ.get(name) or (provider_env or {}).get(name))


def missing_for(cfg: Config, task: Task, prov: str | None = None) -> list[str]:
    """Variables the task needs that are not set. With `prov` the provider that will run it; without,
    every provider the task may use must be able to see the variable (shell, or its `env` table)."""
    if not task.runs_agent:
        return []
    allowed = [p for p in (task.providers or cfg.enabled_providers()) if p in cfg.providers]
    if prov:
        allowed = [prov]
    out: list[str] = []
    for name in list(cfg.required_env) + list(task.required_env):
        if not all(_is_set(name, cfg.providers[p].env) for p in allowed):
            out.append(name)
    for p in allowed:
        pc = cfg.providers[p]
        out += [n for n in pc.required_env if not _is_set(n, pc.env)]
    return list(dict.fromkeys(out))


def explain(task: Task, missing: list[str]) -> str:
    return (f"task {task.id} needs environment variable(s) {', '.join(missing)}, which "
            "are not set in the shell that runs baton (or under [providers.<name>.env]); "
            "without them its tests would be skipped and the result would prove nothing. "
            "Export them and start baton again")


def required_env_errors(cfg: Config, tasks: list[Task]) -> list[str]:
    """Errors for `baton plan`: one per unfinished agent task that lacks a required variable."""
    if not (cfg.required_env or any(t.required_env for t in tasks)
            or any(pc.required_env for pc in cfg.providers.values())):
        return []
    state = load_state(cfg.state_dir / "run.json")
    errors = []
    for t in tasks:
        ts = state.tasks.get(t.id)
        if ts and ts.status in FINISHED:
            continue
        missing = missing_for(cfg, t)
        if missing:
            errors.append(explain(t, missing))
    return errors
