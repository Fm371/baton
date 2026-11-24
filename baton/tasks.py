"""Task files: Markdown with TOML front matter between +++ lines.

    +++
    id = "api-pagination"          # optional, defaults to the file name
    repo = "../shop-api"           # relative to the config folder, or absolute
    work_type = "feature"          # looked up in [routing.work_types]
    # rung = "r3"                  # or name the rung directly
    depends_on = ["db-index"]
    priority = 10                  # higher runs first among ready tasks
    branch = "feat/pagination"     # optional; created from base if missing
    base = "develop"
    timeout_min = 90
    max_turns = 80                 # Claude only
    providers = ["claude"]         # only these run it; no fallback outside the list
    feature = "onboarding"           # optional group; baton plan schedules env-preflight after it
    paths = ["src/app/join/**"]    # optional; paths the task touches (besides those in its text)
    lane = "shared-db"             # optional; with [run] lanes > 1, tasks in one lane never overlap
    +++
    Task prompt in Markdown ...

The body may carry `## User journey` and `## Reuse` sections; `baton plan`
warns when a task that touches UI paths has neither.

Verifier files live in the verifiers folder and carry kind = "verify". They
share the DAG with the tasks but are hidden from implementer runs. A verifier
with `template = "env-preflight" | "browser-smoke" | "real-data-smoke" | "repo-suite"` is run
by baton itself, without an agent; its `params` table feeds the template.

A verifier with `feature = "x"` and `when = "feature-end"` runs once after every
implementer task of that feature instead of gating the tasks that depend on it;
`money_gate = true` marks the verifier that checks money (`baton plan` warns about
more than one per feature).

A task with kind = "approve" is a human checkpoint: baton writes what needs
approval to results/<id>.md and holds its dependents until `baton approve`.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil
from .config import KNOWN_PROVIDERS, Config, ConfigError, env_names
from .pathcheck import expand

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KINDS = ("implement", "verify", "approve")
WHEN_VALUES = ("feature-end",)
TEMPLATES = ("env-preflight", "browser-smoke", "real-data-smoke", "repo-suite")
# every key a task file's front matter may carry (docs/configuration.md lists them all)
TASK_KEYS = {
    "id", "repo", "work_type", "rung", "depends_on", "after", "priority", "branch", "base", "timeout_min", "max_turns",
    "stall_minutes", "return_mid_task", "kind", "providers", "template", "params", "feature", "paths", "when",
    "money_gate", "lane", "needs", "required_env", "review", "settings", "sandbox", "creates_base",
}


class TaskError(Exception):
    pass


@dataclass
class Task:
    id: str
    path: Path
    repo: Path
    body: str
    work_type: str | None = None
    rung: str | None = None
    depends_on: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)   # run order only: wait until these ended, whatever the result;
    #                                                  no gate, no branch containment, no base
    priority: int = 0
    branch: str | None = None
    base: str | None = None
    timeout_min: float | None = None
    max_turns: int | None = None
    return_mid_task: bool | None = None    # overrides [run] return_mid_task for this task (False: never hand it back mid-way)
    stall_minutes: float | None = None     # overrides [run] stall_minutes; 0 turns the watchdog off for this task
    resolved_rung: str = ""
    kind: str = "implement"
    base_explicit: bool = False
    providers: list[str] | None = None   # None: any enabled provider
    template: str | None = None          # a built-in check baton runs itself (verifiers only)
    params: dict = field(default_factory=dict)
    feature: str | None = None
    paths: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)   # variables this task needs set (no silent skipped tests)
    auto: bool = False                   # scheduled by baton plan, not written by a person
    when: str | None = None              # verifiers: "feature-end" runs once after all tasks of its feature
    money_gate: bool = False             # verifiers: marks the gate that checks money; at most one per feature
    lane: str | None = None              # optional: tasks with the same lane never run at the same time
    lane_key: str = ""                   # what excludes: `lane`, else the repository's git top level
    repo_key: str = ""                   # the repository's git top level; one task per repository at a time
    branch_auto: bool = False            # no `branch` in the file: baton uses baton/<id>, from the checkout it finds
    warnings: list[str] = field(default_factory=list)   # notes for `baton plan`, set while resolving bases
    needs: list[str] = field(default_factory=list)       # [services.<name>] that must be healthy before it starts
    settings: str | None = None          # claude settings file for this task (instead of providers.claude.settings)
    sandbox: str | None = None           # codex sandbox mode for this task (instead of providers.codex.sandbox)
    creates_base: bool = False           # this task is meant to create (or commit to) the default base: no main-line warning
    review: bool = False                 # opt in to `baton review` (never run by `baton run`)

    @property
    def lane_name(self) -> str:
        """Short name for logs and `baton watch --lane`."""
        return self.lane or Path(self.repo_key or str(self.repo)).name

    @property
    def is_verifier(self) -> bool:
        return self.kind == "verify"

    @property
    def is_approval(self) -> bool:
        return self.kind == "approve"

    @property
    def runs_agent(self) -> bool:
        """False for human checkpoints and built-in checks: no CLI, no branch."""
        return not self.is_approval and self.template is None

    def section(self, title: str) -> str | None:
        return markdown_section(self.body, title)


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _meta_number(meta: dict, key: str, path: Path, *, integer: bool, minimum: float, default=None,
                 exclusive: bool = False):
    """A numeric front-matter value; a wrong type or range is a TaskError that
    names the file and the key (not a ValueError traceback)."""
    if key not in meta:
        return default
    v = meta[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise TaskError(f"{path.name}: {key} must be {'a whole number' if integer else 'a number'}, got {v!r}")
    if v <= minimum if exclusive else v < minimum:
        raise TaskError(f"{path.name}: {key} must be {'greater than' if exclusive else 'at least'} "
                        f"{minimum:g}, got {v!r}")
    return v


def review_flag(meta: dict, path: Path) -> bool:
    """`review = true|false` in the front matter (default false); a quoted value is a TaskError."""
    value = meta.get("review", False)
    if not isinstance(value, bool):
        raise TaskError(f"{path.name}: review must be true or false (without quotes), not {value!r}")
    return value


def markdown_section(body: str, title: str) -> str | None:
    """The text under a heading called `title` (any level, case-insensitive),
    up to the next heading of the same or a higher level; None when absent
    or empty."""
    level, out = 0, []
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m and not level:
            if m.group(2).strip().lower() == title.lower():
                level = len(m.group(1))
            continue
        if m and len(m.group(1)) <= level:
            break
        if level:
            out.append(line)
    text = "\n".join(out).strip()
    return text or None


def split_front_matter(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise TaskError("must start with a +++ front-matter block")
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            head = "".join(lines[1:i])
            body = "".join(lines[i + 1:]).strip("\n")
            try:
                return tomllib.loads(head), body
            except tomllib.TOMLDecodeError as e:
                raise TaskError(f"front matter: {e}") from e
    raise TaskError("front matter has no closing +++ line")


@dataclass
class _Header:
    """The validated pieces of a task file's front matter, before the Task is built."""
    kind: str
    tid: str
    template: str | None
    when: str | None
    params: dict
    repo: Path
    deps: list[str]
    after: list[str]
    required_env: list[str]
    paths: list[str]
    providers: list[str] | None
    lane: str | None
    needs: list[str]
    return_mid_task: bool | None


def _fail(path: Path, message: str) -> TaskError:
    return TaskError(f"{path.name}: {message}")


def _read_task_file(path: Path) -> tuple[dict, str]:
    try:
        return split_front_matter(path.read_text(encoding="utf-8"))
    except TaskError as e:
        raise TaskError(f"{path.name}: {e}") from e


def _check_known_keys(path: Path, meta: dict) -> None:
    unknown = set(meta) - TASK_KEYS
    if unknown:
        raise _fail(path, f"unknown front-matter keys: {', '.join(sorted(unknown))}")


def _task_kind(path: Path, meta: dict, folder_kind: str) -> str:
    """The kind the file declares, which must match the folder it is in."""
    declared = meta.get("kind", "implement")
    if declared not in KINDS:
        raise _fail(path, f"kind must be one of {', '.join(KINDS)}")
    if folder_kind != "verify" and declared == "verify":
        raise _fail(path, "kind = \"verify\" files belong in the verifiers folder")
    if folder_kind == "verify" and declared == "approve":
        raise _fail(path, "kind = \"approve\" files belong in the tasks folder")
    if folder_kind == "verify" and declared != "verify":
        raise _fail(path, "files in the verifiers folder need kind = \"verify\"")
    return declared


def _task_id(path: Path, meta: dict) -> str:
    tid = str(meta.get("id", path.stem))
    if not ID_RE.match(tid):
        raise _fail(path, f"id '{tid}' may only contain ASCII letters, digits, '.', '_' and '-', "
                          "and must start with a letter or digit")
    return tid


def _task_template(path: Path, meta: dict, kind: str) -> str | None:
    template = meta.get("template")
    if template is not None and (kind != "verify" or template not in TEMPLATES):
        raise _fail(path, f"template must be one of {', '.join(TEMPLATES)} and "
                          "belongs in a kind = \"verify\" file")
    return template


def _task_when(path: Path, meta: dict, kind: str) -> str | None:
    when = meta.get("when")
    if when is None:
        return None
    if when not in WHEN_VALUES or kind != "verify":
        raise _fail(path, "when must be \"feature-end\" and belongs in a kind = \"verify\" file")
    if not meta.get("feature"):
        raise _fail(path, "when = \"feature-end\" needs a feature (the group it runs at the end of)")
    return when


def _check_money_gate(path: Path, meta: dict, kind: str) -> None:
    if meta.get("money_gate") is not None and (kind != "verify" or not isinstance(meta["money_gate"], bool)):
        raise _fail(path, "money_gate = true|false belongs in a kind = \"verify\" file")


def _task_params(path: Path, meta: dict) -> dict:
    params = meta.get("params", {})
    if not isinstance(params, dict):
        raise _fail(path, "params must be a table")
    return params


def _task_repo(path: Path, meta: dict, kind: str, template: str | None, cfg: Config) -> Path:
    # an agent task needs a repository; a checkpoint or built-in check may leave it out
    if "repo" not in meta and (kind == "implement" or (kind == "verify" and template is None)):
        raise _fail(path, "missing repo")
    repo = Path(os.path.expanduser(expand(str(meta.get("repo", ".")), cfg.base_dir)))
    return repo if repo.is_absolute() else cfg.base_dir / repo


def _one_or_many(path: Path, meta: dict, key: str, what: str) -> list[str]:
    """A string or a list of strings (a single name is the same as a list of one)."""
    value = meta.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(path, f"{key} must be {what}, not {value!r}")
    return value


def _check_bool_fields(path: Path, meta: dict) -> None:
    for key in ("creates_base",):
        if key in meta and not isinstance(meta[key], bool):
            raise _fail(path, f"{key} must be true or false (without quotes), not {meta[key]!r}")


def _check_string_fields(path: Path, meta: dict) -> None:
    for key in ("branch", "base", "work_type", "rung", "feature", "settings", "sandbox"):
        if meta.get(key) is not None and not isinstance(meta[key], str):
            raise _fail(path, f"{key} must be a string, not {meta[key]!r}")


def _task_required_env(path: Path, meta: dict) -> list[str]:
    try:
        return env_names(meta.get("required_env"), "required_env")
    except ConfigError as e:
        raise TaskError(f"{path.name}: {e}") from e


def _task_paths(path: Path, meta: dict) -> list[str]:
    paths = meta.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
        raise _fail(path, "paths must be a list of strings")
    return paths


def _task_providers(path: Path, meta: dict, kind: str, template: str | None, cfg: Config) -> list[str] | None:
    providers = meta.get("providers")
    if providers is None:
        return cfg.match_providers(cfg.verifier_providers) if kind == "verify" and template is None else None
    known = dict.fromkeys([*KNOWN_PROVIDERS, *cfg.providers])
    if (not isinstance(providers, list) or not providers
            or not all(isinstance(p, str) and p in known for p in providers)):
        raise _fail(path, f"providers must be a non-empty list of {', '.join(known)}")
    return cfg.match_providers(providers)


def _task_lane(path: Path, meta: dict) -> str | None:
    lane = meta.get("lane")
    if lane is not None and not (isinstance(lane, str) and ID_RE.match(lane)):
        raise _fail(path, f"lane '{lane}' may only contain ASCII letters, digits, '.', '_' and '-', "
                          "and must start with a letter or digit")
    return lane


def _task_needs(path: Path, meta: dict, cfg: Config) -> list[str]:
    needs = _one_or_many(path, meta, "needs", "a service name or a list of service names")
    for n in needs:
        if n not in cfg.services:
            known = ", ".join(sorted(cfg.services)) or "none declared; add a [services.<name>] table"
            raise _fail(path, f"needs '{n}', which is not a [services.*] entry in baton.toml ({known})")
    return needs


def _task_return_mid_task(path: Path, meta: dict) -> bool | None:
    value = meta.get("return_mid_task")
    if value is not None and not isinstance(value, bool):
        raise _fail(path, f"return_mid_task must be true or false (without quotes), not {value!r}")
    return value


def _validate_header(path: Path, meta: dict, body: str, cfg: Config, folder_kind: str) -> _Header:
    """Every check on the front matter, in the order in which problems are reported."""
    kind = _task_kind(path, meta, folder_kind)
    tid = _task_id(path, meta)
    template = _task_template(path, meta, kind)
    when = _task_when(path, meta, kind)
    _check_money_gate(path, meta, kind)
    params = _task_params(path, meta)
    repo = _task_repo(path, meta, kind, template, cfg)
    deps = _one_or_many(path, meta, "depends_on", "a task id or a list of task ids")
    after = _one_or_many(path, meta, "after", "a task id or a list of task ids")
    _check_string_fields(path, meta)
    _check_bool_fields(path, meta)
    if not body.strip() and template is None:
        raise _fail(path, "the task body (prompt) is empty")
    return _Header(
        kind=kind, tid=tid, template=template, when=when, params=params, repo=repo, deps=deps, after=after,
        required_env=_task_required_env(path, meta), paths=_task_paths(path, meta),
        providers=_task_providers(path, meta, kind, template, cfg), lane=_task_lane(path, meta),
        needs=_task_needs(path, meta, cfg), return_mid_task=_task_return_mid_task(path, meta))


def _build_task(path: Path, meta: dict, body: str, cfg: Config, h: _Header) -> Task:
    on_agent = h.kind != "approve" and h.template is None
    return Task(
        id=h.tid, path=path, repo=h.repo.resolve(), body=expand(body, cfg.base_dir),
        work_type=meta.get("work_type"), rung=meta.get("rung"),
        depends_on=[str(d) for d in h.deps], after=[str(d) for d in h.after],
        priority=_meta_number(meta, "priority", path, integer=True, minimum=-1_000_000, default=0),
        branch=meta.get("branch") or (f"verify/{h.tid}" if on_agent and h.kind == "verify" else
                                      f"baton/{h.tid}" if on_agent else None),
        base=meta.get("base") or cfg.default_base,
        timeout_min=_meta_number(meta, "timeout_min", path, integer=False, minimum=0, exclusive=True),
        max_turns=_meta_number(meta, "max_turns", path, integer=True, minimum=0),
        stall_minutes=_meta_number(meta, "stall_minutes", path, integer=False, minimum=0),
        return_mid_task=h.return_mid_task,
        kind=h.kind, base_explicit=bool(meta.get("base")), providers=h.providers,
        template=h.template, params=h.params, feature=meta.get("feature"), paths=h.paths,
        creates_base=bool(meta.get("creates_base", False)), settings=meta.get("settings") or None, sandbox=meta.get("sandbox") or None,
        required_env=h.required_env, when=h.when, money_gate=bool(meta.get("money_gate", False)), lane=h.lane,
        branch_auto=on_agent and h.kind != "verify" and not meta.get("branch"),
        needs=list(h.needs), review=review_flag(meta, path),
    )


def _place_task(task: Task, path: Path, cfg: Config) -> None:
    """Where the task runs: its repository and lane keys, and the rung that picks its models."""
    # a checkpoint touches no repository, so it never holds a lane
    task.repo_key = f"approval:{task.id}" if task.kind == "approve" else str(gitutil.toplevel(task.repo))
    task.lane_key = f"lane:{task.lane}" if task.lane else task.repo_key
    if task.kind != "approve" and task.template is None:
        try:
            task.resolved_rung = cfg.routing.rung_for(task.work_type, task.rung)
        except ConfigError as e:
            raise TaskError(f"{path.name}: {e}") from e


def parse_task_file(path: Path, cfg: Config, kind: str = "implement") -> Task:
    meta, body = _read_task_file(path)
    _check_known_keys(path, meta)
    header = _validate_header(path, meta, body, cfg, kind)
    task = _build_task(path, meta, body, cfg, header)
    _place_task(task, path, cfg)
    return task


def load_tasks(cfg: Config) -> list[Task]:
    if not cfg.tasks_dir.is_dir():
        raise TaskError(f"tasks folder not found: {cfg.tasks_dir}")
    tasks: list[Task] = []
    errors: list[str] = []
    sources = [(cfg.tasks_dir, "implement")]
    if cfg.verifiers_dir.is_dir():
        sources.append((cfg.verifiers_dir, "verify"))
    for folder, kind in sources:
        for p in sorted(folder.glob("*.md")):
            if p.name.startswith(("_", ".")) or p.name.lower() == "readme.md":
                continue
            try:
                tasks.append(parse_task_file(p, cfg, kind))
            except TaskError as e:
                errors.append(str(e))
    seen: dict[str, Path] = {}
    for t in tasks:
        if t.id in seen:
            errors.append(f"duplicate task id '{t.id}' in {seen[t.id].name} and {t.path.name}")
        seen[t.id] = t.path
    if not errors:
        resolve_dependency_bases(tasks)
        errors.extend(resolve_verifier_bases(tasks))
    if errors:
        raise TaskError("\n".join(errors))
    return tasks


def ancestors(task: Task, by_id: dict[str, Task]) -> list[Task]:
    """Every task this one depends on, directly or not."""
    seen: dict[str, Task] = {}
    stack = list(task.depends_on)
    while stack:
        d = stack.pop()
        if d in seen or d not in by_id:
            continue
        seen[d] = by_id[d]
        stack.extend(by_id[d].depends_on)
    return list(seen.values())


def effective_deps(t: Task, by_id: dict[str, Task]) -> list[Task]:
    """What `t` builds on: its dependencies, where an approval checkpoint stands for the tasks it waits for
    (a checkpoint has no branch of its own, so P05 -> A1 -> P04 builds on P04)."""
    out: list[Task] = []
    seen: set[str] = set()
    stack = list(reversed(t.depends_on))
    while stack:
        d = stack.pop()
        if d in seen or d not in by_id:
            continue
        seen.add(d)
        if by_id[d].is_approval:
            stack.extend(reversed(by_id[d].depends_on))
        else:
            out.append(by_id[d])
    return out


def _dependency_branches(t: Task, by_id: dict[str, Task]) -> list[str]:
    """The other branches of the implementer tasks in its own repository that `t` depends on."""
    deps = [d for d in effective_deps(t, by_id) if d.repo == t.repo
            and d.runs_agent and not d.is_verifier and d.branch]
    return sorted({d.branch for d in deps if d.branch != t.branch})


def resolve_dependency_bases(tasks: list[Task]) -> list[str]:
    """A task that depends on a task in its own repository starts its branch from that task's
    branch, so it sees the work it builds on. Only when the dependency is on a different branch,
    exactly one such branch exists, and the file names no `base`. With several different
    branches there is no single answer: the default base stays and `baton plan` warns
    (the text goes to `Task.warnings`)."""
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        if t.is_verifier or not t.runs_agent or t.base_explicit:
            continue
        branches = _dependency_branches(t, by_id)
        if len(branches) == 1:
            t.base = branches[0]
        elif len(branches) > 1:
            t.warnings.append(f"task {t.id}: depends on tasks on different branches ({', '.join(branches)}) "
                              f"in {t.repo.name}; it starts from {t.base or 'the current checkout'}. "
                              "Set `base` to pick one")


def resolve_verifier_bases(tasks: list[Task]) -> list[str]:
    """A verifier branches from the branch of the task it checks: with one
    dependency in its repo, from that task's branch; with several, only when
    they share one branch, unless the verifier names its base explicitly."""
    by_id = {t.id: t for t in tasks}
    errors: list[str] = []
    for v in tasks:
        if not v.is_verifier or v.base_explicit or not v.runs_agent:
            continue
        same_repo = [d for d in effective_deps(v, by_id) if d.repo == v.repo]
        if not same_repo:
            continue
        no_branch = [d.id for d in same_repo if not d.branch]
        if no_branch:
            errors.append(f"verifier {v.id}: dependency {', '.join(no_branch)} has no branch; "
                          "set base explicitly")
            continue
        branches = sorted({d.branch for d in same_repo})
        if len(branches) > 1:
            errors.append(f"verifier {v.id}: dependencies in {v.repo.name} are on different branches "
                          f"({', '.join(branches)}); set base explicitly")
            continue
        v.base = branches[0]
    return errors


def find_cycle(by_id: dict[str, Task], within: set[str]) -> list[str]:
    """One dependency cycle among `within`, as [a, b, ..., a]; [] if there is none."""
    state: dict[str, int] = {}          # 1: on the current path, 2: finished
    path: list[str] = []

    def visit(tid: str) -> list[str]:
        state[tid] = 1
        path.append(tid)
        for d in sorted({*by_id[tid].depends_on, *by_id[tid].after}):
            if d not in within:
                continue
            if state.get(d) == 1:
                return path[path.index(d):] + [d]
            if d not in state:
                found = visit(d)
                if found:
                    return found
        path.pop()
        state[tid] = 2
        return []

    for tid in sorted(within):
        if tid not in state:
            found = visit(tid)
            if found:
                return found
    return []


def cycle_message(by_id: dict[str, Task], remaining: set[str]) -> str:
    """The cycle spelled out, saying which links baton added on its own."""
    cycle = find_cycle(by_id, remaining)
    if not cycle:
        return "dependency cycle among: " + ", ".join(sorted(remaining))

    def label(tid: str) -> str:
        t = by_id[tid]
        return tid + (" (auto-added check)" if t.auto else " (approval checkpoint)" if t.is_approval else "")

    msg = "dependency cycle: " + " -> ".join(label(t) for t in cycle) + "   (each task waits for the next)"
    if any(by_id[t].auto for t in cycle):
        msg += ("\n  baton added the auto check after the last task of a feature, and an approval checkpoint in "
                "that feature waits for it. Move the work that follows the checkpoint to another feature, "
                "leave `feature` off the checkpoint, or turn the check off ([preflight] auto = false).")
    return msg


def order_tasks(tasks: list[Task]) -> list[Task]:
    """Dependencies first; among ready tasks the highest priority, then file name."""
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            if d not in by_id:
                raise TaskError(f"task '{t.id}' depends on unknown task '{d}'")
        for d in t.after:
            if d not in by_id:
                raise TaskError(f"task '{t.id}' has `after` unknown task '{d}'")
    remaining = {t.id for t in tasks}
    done: set[str] = set()
    out: list[Task] = []
    while remaining:
        ready = [by_id[i] for i in remaining if all(d in done for d in (*by_id[i].depends_on, *by_id[i].after))]
        if not ready:
            raise TaskError(cycle_message(by_id, remaining))
        ready.sort(key=lambda t: (-t.priority, t.path.name))
        pick = ready[0]
        out.append(pick)
        done.add(pick.id)
        remaining.discard(pick.id)
    return out
