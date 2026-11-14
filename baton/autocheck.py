"""Checks baton adds to the plan by itself.

After the last task of a feature (tasks sharing a `feature` value) it
schedules an env-preflight verifier, when `[preflight]` is configured, so the
environment gaps are listed before a human starts manual QA. A checkpoint
(`kind = "approve"`) in the feature waits for it. A feature that already has
an env-preflight verifier of its own is left alone.

Likewise one repo-suite verifier per repository that has a `suite_cmd`
(`[repos."<path>"]`), after the last task of the feature that touched that
repository: the full suite runs once there, not inside each task.
"""

from __future__ import annotations

import re

from .config import Config
from .tasks import Task, ancestors


def schedule_preflight(cfg: Config, tasks: list[Task]) -> list[Task]:
    """The tasks plus the checks baton adds by itself."""
    return add_repo_suites(cfg, add_env_preflight(cfg, apply_feature_end(tasks)))


def _by_feature(tasks: list[Task]) -> dict[str, list[Task]]:
    features: dict[str, list[Task]] = {}
    for t in tasks:
        if t.feature:
            features.setdefault(t.feature, []).append(t)
    return features


def _apply_feature_end_to(v: Task, tasks: list[Task], by_id: dict[str, Task]) -> None:
    members = [t for t in tasks if t.feature == v.feature and t is not v]
    before = _before_checkpoints(members, by_id)
    original = list(v.depends_on)
    for m in members:
        if m.is_approval or v.id not in m.depends_on:
            continue
        m.depends_on = [d for d in m.depends_on if d != v.id] + [d for d in original if d not in m.depends_on]
    v.depends_on = list(dict.fromkeys(
        original + [m.id for m in before if m.kind == "implement" and m.runs_agent]))


def apply_feature_end(tasks: list[Task]) -> list[Task]:
    """A verifier with `when = "feature-end"` runs once, after every implementer task
    of its feature (those a checkpoint precedes aside), instead of gating each task
    that depends on it: the feature's own tasks drop their dependency on it and keep
    what it was waiting for."""
    by_id = {t.id: t for t in tasks}
    for v in [t for t in tasks if t.when == "feature-end"]:
        _apply_feature_end_to(v, tasks, by_id)
    return tasks


def _before_checkpoints(members: list[Task], by_id: dict[str, Task]) -> list[Task]:
    """The members that no checkpoint of the feature precedes: work that follows a
    checkpoint is not part of what the checkpoint reviews, and waiting for it
    would deadlock the checkpoint."""
    approvals = [t for t in members if t.is_approval]
    return [t for t in members if not t.is_approval
            and not any(a in ancestors(t, by_id) for a in approvals)]


def _env_preflight_for(cfg: Config, feature: str, members: list[Task], by_id: dict[str, Task]) -> Task | None:
    """The env-preflight verifier to add for one feature (checkpoints made to wait for it), or None."""
    if any(t.template == "env-preflight" for t in members):
        return None
    tid = "env-preflight-" + re.sub(r"[^A-Za-z0-9._-]", "-", feature)
    before = _before_checkpoints(members, by_id)
    if tid in by_id or not before:
        return None
    for a in (t for t in members if t.is_approval):
        if tid not in a.depends_on:
            a.depends_on.append(tid)
    return Task(
        id=tid, path=cfg.verifiers_dir / f"_auto-{tid}.md", repo=cfg.base_dir, body="",
        kind="verify", template="env-preflight", feature=feature, auto=True,
        depends_on=[t.id for t in before],
    )


def add_env_preflight(cfg: Config, tasks: list[Task]) -> list[Task]:
    pc = cfg.preflight
    if not (pc.configured and pc.auto):
        return tasks
    by_id = {t.id: t for t in tasks}
    out = list(tasks)
    for feature, members in _by_feature(tasks).items():
        added = _env_preflight_for(cfg, feature, members, by_id)
        if added is not None:
            out.append(added)
    return out


def _last_of(mine: list[Task]) -> Task:
    """The last task of a repository in a feature: the one no other task of the repository depends on."""
    return [t for t in mine if not any(t.id in o.depends_on for o in mine if o is not t)][-1]


def _suite_task(cfg: Config, feature: str, path, tid: str, mine: list[Task]) -> Task:
    last = _last_of(mine)
    return Task(
        id=tid, path=cfg.verifiers_dir / f"_auto-{tid}.md", repo=path, body="", kind="verify",
        template="repo-suite", feature=feature, auto=True, depends_on=[t.id for t in mine],
        params={"branch": last.branch, "base": last.base} if last.branch else {},
    )


def _suite_wanted(members: list[Task], before: list[Task], path) -> list[Task]:
    """The tasks of `path` a repo-suite would follow; [] when there are none or the feature has its own."""
    mine = [t for t in before if t.runs_agent and not t.is_verifier and t.repo == path]
    own = any(t.template == "repo-suite" and t.repo == path for t in members)
    return [] if own else mine


def _wire_checkpoints(members: list[Task], tid: str, ids: set[str], by_id: dict[str, Task]) -> None:
    """Checkpoints that review the repository's tasks wait for its suite too."""
    for a in (t for t in members if t.is_approval):
        if tid not in a.depends_on and ids & {x.id for x in ancestors(a, by_id)}:
            a.depends_on.append(tid)


def _repo_suite_for(cfg: Config, feature: str, members: list[Task], path, before: list[Task],
                    by_id: dict[str, Task]) -> Task | None:
    """The repo-suite verifier for one repository of one feature (checkpoints made to wait for it), or None."""
    mine = _suite_wanted(members, before, path)
    tid = "repo-suite-" + re.sub(r"[^A-Za-z0-9._-]", "-", f"{feature}-{path.name}")
    if not mine or tid in by_id:
        return None
    suite = _suite_task(cfg, feature, path, tid, mine)
    by_id[tid] = suite
    _wire_checkpoints(members, tid, {t.id for t in mine}, by_id)
    return suite


def add_repo_suites(cfg: Config, tasks: list[Task]) -> list[Task]:
    suites = {path: rc for path, rc in cfg.repos.items() if rc.suite_cmd}
    if not suites:
        return tasks
    by_id = {t.id: t for t in tasks}
    out = list(tasks)
    for feature, members in _by_feature(tasks).items():
        before = _before_checkpoints(members, by_id)
        for path in suites:
            suite = _repo_suite_for(cfg, feature, members, path, before, by_id)
            if suite is not None:
                out.append(suite)
    return out
