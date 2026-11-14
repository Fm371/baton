"""What an operator does to a run from outside: `baton mark`, `baton retry`, `baton providers reset`,
and the hints `baton status` prints about how to get a stuck run moving.

Only the runner writes state/run.json while it is running. So with no run active these commands take
the run lock and edit the state themselves; with a run active they leave a small file in state/ops/,
which the runner applies between tasks (`baton mark`, `baton providers reset`). `baton retry` needs
the run to be stopped, because it starts a run of its own."""

from __future__ import annotations

import fcntl
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import gitutil
from .autocheck import schedule_preflight
from .config import Config
from .scheduler import ProviderStatus
from .state import atomic_write
from .tasks import Task, TaskError, ancestors, load_tasks, order_tasks
from .watch import active_message, run_active

MARK_STATUSES = ("done", "partial", "failed", "blocked")   # partial: finished, but not a pass (retires a verifier)


class OpsError(Exception):
    """A refusal with a message for the operator."""


@contextmanager
def exclusive(cfg: Config):
    """The run lock, held for the length of an operator edit. Refuses when a run holds it."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    f = (cfg.state_dir / "lock").open("a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OpsError(active_message(cfg)) from None
        yield
    finally:
        f.close()


def load_ordered(cfg: Config) -> list[Task]:
    try:
        return order_tasks(schedule_preflight(cfg, load_tasks(cfg)))
    except TaskError as e:
        raise OpsError(f"task error: {e}") from None


def find_task(ordered: list[Task], tid: str) -> Task:
    task = next((t for t in ordered if t.id == tid), None)
    if task is None:
        known = ", ".join(t.id for t in ordered[:12]) + (" …" if len(ordered) > 12 else "")
        raise OpsError(f"no task with id '{tid}' (known: {known})")
    return task


def _runner(cfg: Config, ordered: list[Task]):
    from .runner import Runner      # not at import time: the runner imports this package's modules
    r = Runner(cfg, echo=False)
    r.ordered = ordered
    return r


# ------------------------------------------------------------------ mark


def mark_text(task: Task, status: str, note: str, result_file: Path | None, result_path: Path) -> str | None:
    """What results/<id>.md should say after a mark; None: leave the file as it is."""
    if result_file is not None:
        if not result_file.is_file():
            raise OpsError(f"--result {result_file}: no such file")
        return result_file.read_text(encoding="utf-8", errors="replace")
    if status not in ("done", "blocked"):
        return None
    if not result_path.is_file():
        return (f"Marked {status} by the operator" + (f": {note}" if note else ".") + "\n") if task.runs_agent else None
    # the file says what the agent concluded; the first line must now say what the operator decided
    old = result_path.read_text(encoding="utf-8", errors="replace")
    first, _, rest = old.partition("\n")
    if not first.startswith("STATUS:") or first.strip() == f"STATUS: {status}":
        return None
    return (f"STATUS: {status}\n" + rest.rstrip("\n") + f"\n\n(Status set to {status} by the operator"
            + (f": {note}" if note else "") + f"; the agent had written: {first.strip() or 'nothing'}.)\n")


def _write_marked_result(cfg: Config, task: Task, status: str, text: str, result_path: Path) -> None:
    if result_path.is_file():           # keep what was there, in the task's own folder
        keep = cfg.results_dir / task.id
        keep.mkdir(parents=True, exist_ok=True)
        n = 1
        while (keep / f"result-before-mark-{n}.md").exists():
            n += 1
        shutil.copy2(result_path, keep / f"result-before-mark-{n}.md")
    if status in ("done", "blocked") and not text.lstrip().startswith("STATUS:"):
        text = f"STATUS: {status}\n\n{text}"
    atomic_write(result_path, text)


def _free_dependents(r, task: Task) -> list[str]:
    """Tasks that were held back by this one (gated or skipped) become pending again."""
    by_id = {t.id: t for t in r.ordered}
    freed = []
    for t in r.ordered:
        ds = r.state.task(t.id)
        if t.id != task.id and ds.status in ("gated", "skipped") and not ds.rejected_by \
                and task.id in {a.id for a in ancestors(t, by_id)}:
            ds.status, ds.note, ds.gated_by = "pending", "", None
            freed.append(t.id)
    r._freed_ids = getattr(r, "_freed_ids", set()) | set(freed)     # a live run queues them again
    return freed


def apply_mark(r, task: Task, status: str, note: str, result_file: Path | None) -> str:
    """Set a task's status by hand on Runner `r` (offline, or the live runner between tasks).
    Returns a line for the operator."""
    cfg = r.cfg
    ts = r.state.task(task.id)
    if ts.status == "running" and r.run_is_live:
        raise OpsError(f"{task.id} is running an attempt right now; mark it after the attempt ends "
                       "(or `baton stop` first)")
    result_path = cfg.results_dir / f"{task.id}.md"
    text = mark_text(task, status, note, result_file, result_path)
    if text is not None:
        _write_marked_result(cfg, task, status, text, result_path)
    ts.status = status
    ts.note = f"marked {status} by operator" + (f": {note}" if note else "")
    ts.overridden_by = "operator"
    ts.finished = r.clock.now().isoformat()
    ts.gated_by = ts.rejected_by = None
    if task.runs_agent and ts.start_head and gitutil.is_repo(task.repo):
        try:
            ts.commits = gitutil.count_commits(task.repo, ts.start_head)
        except gitutil.GitError:
            pass
    freed = _free_dependents(r, task) if status == "done" else []
    r.save()
    r.write_status_file(task)
    r.log(f"task {task.id}: marked {status} by operator" + (f" ({note})" if note else "")
          + (f"; ungated {', '.join(freed)}" if freed else ""))
    r.write_summary(r.ordered)
    return (f"{task.id} is now {status}"
            + (f"; {', '.join(freed)} can run again" if freed else "")
            + ("; run `baton run` to continue" if freed else ""))


def mark(cfg: Config, tid: str, status: str, note: str, result_file: str | None) -> str:
    if status not in MARK_STATUSES:
        raise OpsError(f"status must be one of {', '.join(MARK_STATUSES)}")
    ordered = load_ordered(cfg)
    task = find_task(ordered, tid)
    path = Path(result_file).resolve() if result_file else None
    if run_active(cfg):
        queue_op(cfg, {"op": "mark", "task": tid, "status": status, "note": note,
                       "result": str(path) if path else None})
        return (f"a run is active: the mark of {tid} as {status} is queued and applied between tasks "
                "(not while this task's attempt is running)")
    with exclusive(cfg):
        return apply_mark(_runner(cfg, ordered), task, status, note, path)


# ------------------------------------------------------------------ retry


def clear_for_retry(r, task: Task, how: str) -> str:
    """Put a task back to pending on Runner `r`; returns the status it had. The old attempt files move
    aside when it starts again."""
    ts = r.state.task(task.id)
    before = ts.status
    ts.status, ts.note, ts.finished = "pending", "", None
    ts.gated_by = ts.rejected_by = ts.overridden_by = None
    ts.quick_fails = 0
    r.state.quick_fails, r.state.quick_tasks = 0, []
    r._freed_ids = getattr(r, "_freed_ids", set()) | {task.id}      # a live run queues it again
    r.save()
    r.log(f"task {task.id}: retry requested by the operator{how} (was {before})")
    r.write_summary(r.ordered)
    return before


def apply_retry(r, task: Task) -> str:
    """`baton retry` typed while this run is active: the live runner clears the task between tasks and
    runs it in this run."""
    if r.state.task(task.id).status == "running" and r.run_is_live:
        raise OpsError(f"{task.id} is running an attempt right now; retry it after the attempt ends")
    return clear_for_retry(r, task, " (queued into the live run)")


def prepare_retry(cfg: Config, tid: str) -> str | None:
    """Clear a task's state so `run --only` starts it fresh. The old attempt files move aside when it starts.
    With a run active the retry is queued into that run instead, and None is returned."""
    ordered = load_ordered(cfg)
    task = find_task(ordered, tid)
    if task.is_approval:
        raise OpsError(f"{tid} is an approval checkpoint: use `baton approve {tid}` or `baton reject {tid}`")
    if run_active(cfg):
        queue_op(cfg, {"op": "retry", "task": tid})
        return None
    with exclusive(cfg):
        return clear_for_retry(_runner(cfg, ordered), task, "")


# ------------------------------------------------------------------ providers


def providers_reset(cfg: Config, name: str) -> str:
    if name not in cfg.providers:
        raise OpsError(f"unknown provider '{name}' (known: {', '.join(cfg.providers)})")
    if run_active(cfg):
        queue_op(cfg, {"op": "providers_reset", "provider": name})
        return f"a run is active: the reset of {name} is queued and applied between tasks"
    ordered = load_ordered(cfg)
    with exclusive(cfg):
        r = _runner(cfg, ordered)
        apply_providers_reset(r, name)
    return f"{name}: limit and disabled flag cleared; the next run treats it as available"


def apply_providers_reset(r, name: str) -> None:
    r.sched.status[name] = ProviderStatus()
    r.log(f"provider {name}: limit and disabled flag cleared by the operator")
    r.save()


# ------------------------------------------------------------------ ops files for a run that is active


def queue_op(cfg: Config, op: dict) -> None:
    d = cfg.state_dir / "ops"
    atomic_write(d / f"{time.time_ns()}-{uuid.uuid4().hex[:6]}.json", json.dumps(op) + "\n")


def take_ops(cfg: Config) -> list[dict]:
    """The queued operator commands, oldest first; the files are removed as they are read."""
    d = cfg.state_dir / "ops"
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        p.unlink(missing_ok=True)
    return [o for o in out if isinstance(o, dict)]


# ------------------------------------------------------------------ hints for `baton status`


def _hint_for(t: Task, state, by_id: dict) -> str | None:
    """The one command that would move this task on, or None."""
    ts = state.task(t.id)
    if t.is_approval and ts.status == "awaiting":
        return (f"{t.id} waits for you: read results/{t.id}.md, then `baton approve {t.id}` "
                f"or `baton reject {t.id} --note ...`")
    if ts.status in ("partial", "failed", "blocked") and not ts.rejected_by and not t.is_approval:
        return (f"{t.id} is {ts.status}: `baton retry {t.id}` runs it again; if the work is fine as it is "
                f"(the cause was elsewhere), `baton mark {t.id} done --note \"why\"`")
    if ts.status in ("gated", "skipped") and ts.gated_by and ts.gated_by in by_id:
        g = ts.gated_by
        return (f"{t.id} is held by verifier {g} ({state.task(g).status}): fix the cause and "
                f"`baton retry {g}`, or accept it with `baton mark {g} done --note \"why\"`")
    if ts.status == "rejected":
        return f"{t.id} was rejected: `baton approve {t.id}` releases what it holds"
    return None


def hints(ordered: list[Task], state, active: bool) -> list[str]:
    """How to move a stuck run on, in plain commands. Empty when nothing needs a person."""
    by_id = {t.id: t for t in ordered}
    out = [h for h in (_hint_for(t, state, by_id) for t in ordered) if h]
    if not active and any(state.task(t.id).status in ("pending", "running", "waiting", "stopped")
                          for t in ordered):
        out.append("`baton run` continues the run")
    seen = set()
    return [h for h in out if not (h in seen or seen.add(h))][:12]
