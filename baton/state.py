"""Run state on disk, written atomically after every transition so a crash or
reboot resumes where it stopped."""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .scheduler import ProviderStatus

FINISHED = ("done", "partial")


class StateError(Exception):
    """run.json cannot be read and there is no usable backup."""


class DiskFull(Exception):
    """A write failed because the disk (or quota) is full. `where` is the folder."""

    def __init__(self, where: str):
        super().__init__(f"disk full in {where}")
        self.where = where


def is_disk_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)


@dataclass
class TaskState:
    status: str = "pending"        # pending|running|waiting|done|partial|blocked|failed|skipped|gated|stopped|awaiting|rejected
    attempts: int = 0
    start_head: str | None = None
    providers: list[str] = field(default_factory=list)
    commits: int = 0
    started: str | None = None
    finished: str | None = None
    note: str = ""
    last_end: str = ""             # why the previous attempt ended, for the next prompt
    gated_by: str | None = None    # the verifier whose result held this task back
    rejected_by: str | None = None # the approval checkpoint whose rejection blocked this task
    quick_fails: int = 0           # attempts of this task that ended fast for no known reason, in a row
    limit_cycles: int = 0          # usage-limit outcomes in a row that left the repository where it was
    limit_head: str | None = None  # the commit the repository was at after the last of them
    suspect_files: list = field(default_factory=list)   # secret-looking files a WIP commit took (run.wip_warn_patterns)
    reason: str = ""               # why it ended this way: "env" (the environment, not the code) or "finding"
    overridden_by: str | None = None   # "operator" when `baton mark` set the status by hand
    implemented_by: str = ""       # the provider that finished the task (set once it is done); `baton review` uses the other
    review_verdict: str = ""       # "" | approve | changes | reject | invalid: the last `baton review`
    review_by: str = ""            # the provider that wrote it
    review_at: str = ""
    fix_round: int = 0             # `baton review-fix` runs started for this task (one allowed without --force)
    fix_state: str = ""            # "" | running | done | blocked


@dataclass
class RunState:
    current: str | None = None
    providers: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, TaskState] = field(default_factory=dict)
    quick_fails: int = 0
    quick_tasks: list[str] = field(default_factory=list)   # the tasks those quick exits belonged to
    # live work, filled only when [run] lanes > 1: task id -> {lane, provider, attempt, out}
    running: dict[str, dict] = field(default_factory=dict)
    # agent processes alive right now: task id -> {pid, pgid, start, repo}. A run that dies
    # without cleaning up (SIGKILL, power loss) leaves them here; the next run finds and ends them.
    agents: dict[str, dict] = field(default_factory=dict)
    # repository path -> the branch it was on before baton first switched it; put back when the run ends
    # (a detached HEAD is "detached:<commit>")
    initial_branches: dict[str, str] = field(default_factory=dict)
    # repository path -> a branch an agent left the repository on by mistake: never the user's own branch
    stray_branches: dict[str, str] = field(default_factory=dict)

    recovered_from: str | None = field(default=None, repr=False)     # not saved: set when run.json.bak was used
    baton_version: str = ""        # what wrote this state: "0.3.0 (abc1234)"

    def task(self, tid: str) -> TaskState:
        if tid not in self.tasks:
            self.tasks[tid] = TaskState()
        return self.tasks[tid]

    def provider_status(self) -> dict[str, ProviderStatus]:
        return {p: ProviderStatus.from_dict(d) for p, d in self.providers.items()}


def expire_limits(st: RunState, now) -> list[str]:
    """Drop every provider limit (and per-model limit) whose reset time has passed, so no summary or plan
    shows 'limited until <a time in the past>'. A provider that was disabled stays disabled. Returns one
    line per limit that expired, for the log."""
    from datetime import datetime
    gone: list[str] = []

    def past(value) -> bool:
        try:
            when = datetime.fromisoformat(value) if isinstance(value, str) else value
            if when.tzinfo is None:
                return False
            return when <= now
        except (TypeError, ValueError):
            return False

    for name, d in st.providers.items():
        if d.get("limited_until") and past(d["limited_until"]):
            gone.append(f"{name}: its limit ({d.get('window') or 'limit'}, until {d['limited_until']}) has passed")
            d["limited_until"] = None
            d["window"] = ""
            d["reset_known"] = True
        models = d.get("model_limits") or {}
        for fam in [m for m, u in models.items() if past(u)]:
            gone.append(f"{name}: its {fam} limit (until {models[fam]}) has passed")
            del models[fam]
    return gone


def _fsync_dir(path: Path) -> None:
    """Make a rename durable: the directory entry itself must reach the disk (power loss)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError as e:
        if is_disk_full(e):
            raise DiskFull(str(path.parent)) from e
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if is_disk_full(e):
            raise DiskFull(str(path.parent)) from e
        raise


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _parse_state(text: str) -> RunState:
    d = json.loads(text)
    if not isinstance(d, dict):
        raise ValueError("run.json is not an object")
    st = RunState(current=d.get("current"), providers=d.get("providers", {}),
                  quick_fails=int(d.get("quick_fails", 0)),
                  quick_tasks=list(d.get("quick_tasks", [])), running=dict(d.get("running", {})),     # version 1: none
                  agents=dict(d.get("agents", {})),
                  initial_branches=dict(d.get("initial_branches", {})),
                  stray_branches=dict(d.get("stray_branches", {})),
                  baton_version=str(d.get("baton_version", "")))
    known = set(TaskState.__dataclass_fields__)
    for tid, t in d.get("tasks", {}).items():
        st.tasks[tid] = TaskState(**{k: v for k, v in t.items() if k in known})
    return st


def load_state(path: Path) -> RunState:
    """The saved state. A run.json that cannot be read falls back to run.json.bak (the previous good
    copy; `recovered_from` on the result says so); with no usable backup it is a StateError that
    says what to do, not a traceback."""
    if not path.is_file():
        return RunState()
    try:
        return _parse_state(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, AttributeError) as first:
        bak = backup_path(path)
        try:
            st = _parse_state(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError, AttributeError):
            raise StateError(
                f"{path} is damaged ({type(first).__name__}: {first}) and there is no usable backup. "
                f"Move it aside (mv {path} {path}.broken) to start the run state over; the finished "
                f"tasks' result files in results/ are untouched, and `baton run --only ID` reruns one task."
            ) from first
        st.recovered_from = bak.name
        return st


def _keep_backup(path: Path) -> None:
    """Copy the current run.json to run.json.bak, unless it is itself damaged."""
    try:
        _parse_state(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return
    bak = backup_path(path)
    tmp = bak.with_name(bak.name + ".tmp")
    try:
        shutil.copyfile(path, tmp)
        os.replace(tmp, bak)
    except OSError as e:
        if is_disk_full(e):
            raise DiskFull(str(path.parent)) from e


def record_implementers(st: RunState) -> None:
    """A finished task remembers which provider did the work (the last one that ran it), once. `baton
    review` reviews with the other provider, so it must still know after later runs and limits."""
    for ts in st.tasks.values():
        if ts.status in FINISHED and not ts.implemented_by and ts.providers:
            ts.implemented_by = ts.providers[-1]


def save_state(path: Path, st: RunState) -> None:
    record_implementers(st)
    if path.is_file():
        _keep_backup(path)
    d = {"version": 2, "current": st.current, "providers": st.providers, "quick_fails": st.quick_fails,
         "quick_tasks": st.quick_tasks,
         "running": st.running, "agents": st.agents, "initial_branches": st.initial_branches,
         "stray_branches": st.stray_branches,
         "baton_version": st.baton_version,
         "tasks": {tid: asdict(t) for tid, t in st.tasks.items()}}
    atomic_write(path, json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def mid_attempt(st: RunState) -> list[str]:
    """Tasks that started an attempt and are not finished: they continue on the current provider."""
    return [tid for tid, t in st.tasks.items()
            if t.attempts > 0 and (t.status == "running" or (t.status == "waiting" and t.providers))]


def starting_provider(st: RunState, start: str, override: str | None = None) -> str:
    """The provider a run begins on. The state's `current` only matters while a
    task is mid-attempt; otherwise `start_provider` (or the --provider override) wins."""
    if override:
        return override
    if st.current and mid_attempt(st):
        return st.current
    return start
