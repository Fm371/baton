"""The run loop: tasks in order, one fresh CLI session per attempt, provider
switching on usage limits, and an emergency brake for unexplained exits."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import envlimit, gitutil, sandbox, services, timefmt, versioninfo
from .autocheck import schedule_preflight
from .checks import keycheck
from .clock import Clock, local_zone
from .config import Config
from .handoff import HandoffMixin
from .lanes import LanesMixin
from .finishing import FinishFacts, decide_finish
from .isolation import outside_test_paths
from .limits import Outcome, classify, read_output, unmatched_failure_warning
from .notify import TASK_EVENTS, Notifier
from .process import Attempt, EmergencyBrake, ProcessMixin, RunStopped  # noqa: F401 (Attempt re-exported)
from .requiredenv import explain, missing_for, skipped_for_env
from .providers import home_dir
from .report import ReportMixin
from .results import read_codex_status, read_status
# re-exported: these names used to live in this module
from .results import COMMIT_RE, HEADER_MAX, STATUS_RE, read_commit_message  # noqa: F401
from .scheduler import Pick, Scheduler
from .state import (FINISHED, DiskFull, RunState, expire_limits, is_disk_full, load_state, mid_attempt, save_state,
                    starting_provider)
from .taskqueue import QueueMixin
from .tasks import Task, ancestors, load_tasks, order_tasks
from .watch import active_message


def _tail_text(path: Path, limit: int = 2_000_000) -> str:
    """The last `limit` bytes of a file as text (the end of an agent's output is where its errors are)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


_GO, _AGAIN, _DONE = "go", "again", "done"     # what a step of run_task tells its loop
_REQUEUE = "requeue"        # lanes: give the task back to the dispatcher, which picks its next provider


def _codex_why(att: Attempt, oc, status: str | None) -> str:
    """The WIP commit reason for what a Codex attempt left behind."""
    if status:
        return "left uncommitted at the end of the task"
    if oc.kind == "limit":
        return "codex hit its usage limit"
    if oc.kind == "auth":
        return "codex authentication error"
    if att.timed_out:
        return "timed out"
    if att.stalled:
        return "stalled (no output)"
    return f"ended without a result file (exit {att.exit_code})"


def _keys_message(found: list) -> str:
    """What the agent is told when baton finds possible hardcoded data keys in its work."""
    forbidden = [f for f in found if f.name == keycheck.FORBIDDEN]
    return ("baton found "
            + ("string literals that look like hardcoded data keys" if len(forbidden) < len(found) else "")
            + (" and " if forbidden and len(forbidden) < len(found) else "")
            + ("literals this repository forbids (`forbid_literals` in baton.toml)" if forbidden else "")
            + ":\n" + "\n".join(f"  {f}" for f in found[:10])
            + "\nResolve each one from config or from the database, or justify it under a "
              "`## Hardcoded keys` heading in the result file. Then write the result file again.")


@dataclass
class _TaskLoop:
    """What one run_task call carries from attempt to attempt."""
    result_path: Path
    branch_at_start: str | None
    no_result_left: int
    transient_left: int
    keys_left: int
    fresh: bool
    held_status: str | None = None     # the verdict of an attempt baton asked to look at its literals again
    stall_left: int = 1                # retries after an attempt whose output stopped growing
    fixed: str | None = None           # lanes: the provider the dispatcher assigned; None when lanes = 1
    avoid: str | None = None           # env-limited: the provider whose sandbox blocked the last attempt
    env_retried: bool = False          # the one retry on the other provider is spent
    env_note: str = ""                 # "env-limited: ..." for the note of the task's final status


@dataclass
class _Tried:
    """One finished attempt, as read back by _run_attempt."""
    prov: str
    n: int
    att: Attempt
    oc: Outcome
    status: str | None
    weak_note: str
    now: datetime
    returned: str | None = None        # the attempt was stopped so the task goes back to this provider


class Runner(QueueMixin, HandoffMixin, ProcessMixin, ReportMixin, LanesMixin):
    def __init__(self, cfg: Config, clock: Clock | None = None, only: str | None = None,
                 echo: bool = True, force: bool = False, provider: str | None = None,
                 follow: bool = False):
        self.cfg = cfg
        self.clock = clock or Clock()
        self.only = only
        self.force = force
        self.follow = follow and not only      # keep running when the queue is empty
        self.echo = echo
        self.tz = cfg.timezone or local_zone()
        self.state_path = cfg.state_dir / "run.json"
        self.state: RunState = load_state(self.state_path)
        self._expired_limits = expire_limits(self.state, self.clock.now())   # logged once the log is ready
        self.disk_full: DiskFull | None = None      # set when a write failed for lack of space
        enabled = cfg.enabled_providers()
        self.start = provider or cfg.start_provider
        if self.start not in enabled:
            raise ValueError(f"provider '{self.start}' is not enabled")
        order = [self.start] + [p for p in enabled if p != self.start]
        self.sched = Scheduler([p for p in order if p in enabled],
                               starting_provider(self.state, cfg.start_provider, provider),
                               self.state.provider_status(),
                               return_to_preferred=cfg.return_to_start_provider)
        self.stop_reason: str | None = None
        self.stop_requested = False      # a signal: the running attempt is stopped
        self.stop_file = cfg.state_dir / "stop"     # `baton stop`: finish the task in progress, then end
        self.graceful_stop = False
        self._children: dict[int, object] = {}     # thread id -> its running agent process
        self._lock = threading.RLock()             # state, SUMMARY.md, run.log and metrics.jsonl, one writer at a time
        self._tl = threading.local()               # lanes: which task / lane this worker thread runs
        self._acts: dict[str, str] = {}            # task id (or "*") -> what it is doing now, for SUMMARY.md
        self._busy_repos: set[str] = set()         # lanes: repositories a worker is in right now
        self._budgets: dict[str, tuple] = {}       # lanes: retries a task has left when it goes back to the dispatcher
        self._crash: BaseException | None = None
        self._ended = False                        # the run is over: a task still `running` was interrupted
        self._cfg_seen = self.config_snapshot()      # baton.toml as it is now, to tell later edits from the rest
        self.run_is_live = False                   # True while run() holds the lock (not for an operator's edit)
        self._reload_error: str | None = None
        self.run_id = self.clock.now().astimezone(self.tz).strftime("%Y%m%d-%H%M%S")
        self.verifiers: list[Task] = []
        self.ordered: list[Task] = []
        self._others: list[Task] = []     # other ready tasks, for skip-ahead
        self.hook_failed: dict[str, str] = {}   # task id -> hook output, when baton's commit was refused
        self.known_leftovers: dict[str, set[str]] = {}   # repository -> uncommittable paths already reported
        self.commit_failed: dict[str, str] = {}  # repository -> why the last WIP commit failed, until one works
        self.refused: dict[str, list[str]] = {}  # task id -> files kept out of its commits (look like secrets)
        self.notifier = Notifier(cfg.on_event, cfg.on_event_timeout_s, self.run_id, lambda m: self.log(m))

    # ------------------------------------------------------------ plumbing

    @property
    def lanes_on(self) -> bool:
        """Several tasks at once. `--only` runs one task, so it never needs more than one lane."""
        return self.cfg.lanes > 1 and not self.only

    @property
    def lane(self) -> str | None:
        return getattr(self._tl, "lane", None)

    @property
    def activity(self) -> str | None:
        return self._acts.get(getattr(self._tl, "task", None) or "*")

    @activity.setter
    def activity(self, value: str | None) -> None:
        key = getattr(self._tl, "task", None) or "*"
        if value is None:
            self._acts.pop(key, None)
        else:
            self._acts[key] = value

    def activity_of(self, task_id: str) -> str | None:
        """What a running task is doing: its own note with lanes, else the one note of the run."""
        return self._acts.get(task_id) if self.lanes_on else self._acts.get("*")

    def log(self, msg: str) -> None:
        with self._lock:
            lane = f"[{self.lane}] " if self.lanes_on and self.lane else ""
            line = f"[{self.clock.now().astimezone(self.tz):%Y-%m-%d %H:%M:%S}] {lane}{msg}"
            if self.disk_full is None:          # after a full disk the log is console-only
                try:
                    self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
                    with (self.cfg.logs_dir / "run.log").open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError as e:
                    if not is_disk_full(e):
                        raise
                    self.disk_full = DiskFull(str(self.cfg.logs_dir))
                    print(line, flush=True)
                    raise self.disk_full from e
            if self.echo:
                try:
                    print(line, flush=True)
                except OSError:         # the terminal is gone (SIGHUP, ssh dropped): keep running, log only
                    self.echo = False

    def save(self) -> None:
        with self._lock:
            d = self.sched.to_dict()
            # `current` only matters while a task is mid-attempt (see starting_provider); between
            # tasks and at a clean end it is cleared, so the next start has no stale value to warn about
            self.state.current = d["current"] if mid_attempt(self.state) else None
            # an auth problem is re-checked on the next start (you may have logged in)
            self.state.providers = {p: {**s, "disabled": None} for p, s in d["providers"].items()}
            save_state(self.state_path, self.state)

    def refresh(self, activity: str | None) -> None:
        """Save the state and rewrite SUMMARY.md, so `baton status` shows what
        the running task is doing (attempt, provider, sleep) and not only how
        finished tasks ended."""
        with self._lock:
            self.activity = activity
            self.save()
            self.write_summary(self.ordered)

    def stop_wanted(self) -> bool:
        """A signal, or `baton stop`. The second one never interrupts an attempt;
        it ends the run between tasks and cuts a wait for a reset short."""
        if self.stop_requested:
            return True
        if self.stop_file.exists():
            self.graceful_stop = True
            self.stop_reason = self.stop_reason or "stop requested with `baton stop`"
            return True
        return False

    def _signal(self, signum, _frame) -> None:
        again = self.stop_requested
        self.stop_requested = True
        self.stop_reason = self.stop_reason or f"stopped by signal {signum}"
        # every lane's agent; each worker then commits its leftovers. A second signal
        # does not wait for an agent that ignored the first: SIGKILL.
        self.kill_children(force=again)

    # ------------------------------------------------------------ run

    def run(self, tasks: list[Task] | None = None) -> int:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        # "a+", not "w": opening must not erase what the run that holds the lock wrote into it
        lock = (self.cfg.state_dir / "lock").open("a+")
        for attempt in range(10):         # a `baton status` probe holds a shared lock for a moment
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 9:
                    lock.close()
                    print(active_message(self.cfg), flush=True)
                    return 4
                time.sleep(0.1)
        # who holds the lock, for the next `baton run` that finds it taken (the lock itself dies with us)
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({"pid": os.getpid(), "run": self.run_id,
                               "started": timefmt.iso(self.clock.now(), self.tz)}))
        lock.flush()
        self.run_is_live = True
        self.stop_file.unlink(missing_ok=True)      # left by a run that crashed
        # SIGHUP: the terminal closed (ssh dropped, no tmux). Stop like Ctrl+C, and commit the work.
        old = {s: signal.signal(s, self._signal) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            return self._run(tasks)
        except DiskFull as e:
            return self.disk_full_exit(e)
        except BaseException as e:
            self.notify("run_died", note=f"baton crashed: {type(e).__name__}: {e}")
            raise
        finally:
            self.run_is_live = False
            self.notifier.wait()
            try:
                self.restore_branches()
            except DiskFull:
                pass                    # the disk is still full: the repositories keep their task branches
            for s, h in old.items():
                signal.signal(s, h)
            self.stop_file.unlink(missing_ok=True)
            lock.close()

    def disk_full_exit(self, e: DiskFull) -> int:
        """A write failed for lack of space. Nothing more is written; run.json is as the last good save
        left it, so `baton run` after freeing space continues where this stopped."""
        self.disk_full = self.disk_full or e
        print(f"baton: disk full in {e.where}. The run stopped; run.json is intact. "
              f"Free space (old results/*/attempt-*.out and logs/ are safe to delete), then run baton again.",
              file=sys.stderr, flush=True)
        return 6

    def _run(self, tasks: list[Task] | None) -> int:
        ordered = order_tasks(tasks if tasks is not None else schedule_preflight(self.cfg, load_tasks(self.cfg)))
        by_id = {t.id: t for t in ordered}
        self._begin_run(ordered)
        refused = self._check_only(by_id)
        if refused is not None:
            return refused
        self._log_run_start(ordered)
        decided = self._recover_previous_run(ordered)
        pending = self._pending_tasks(ordered, by_id, decided)
        seen = {t.id for t in ordered}
        if self.lanes_on:
            ordered, by_id = self.dispatch(ordered, by_id, pending, seen)
        else:
            ordered, by_id = self.serial_loop(ordered, by_id, pending, seen)
        return self._end_run(ordered, pending)

    def _begin_run(self, ordered: list[Task]) -> None:
        """The first facts of a run: the task order, this baton's version, limits that expired meanwhile."""
        self.ordered = ordered
        self.state.baton_version = versioninfo.describe()
        for line in self._expired_limits:
            self.log(f"limit expired: {line}")
        self._expired_limits = []
        if not any(s.status in ("running", "waiting") for s in self.state.tasks.values()):
            self._forget_stale_branches()
        self.verifiers = [t for t in ordered if t.is_verifier]

    def _forget_stale_branches(self) -> None:
        """Nothing is mid-way, so what was recorded about the branches is stale, except where an agent left a
        repository on a branch of its own and the user's branch has not been put back yet."""
        for key in [k for k in self.state.initial_branches if k not in self.state.stray_branches]:
            del self.state.initial_branches[key]
        for key in [k for k in self.state.stray_branches if k not in self.state.initial_branches]:
            del self.state.stray_branches[key]

    def _check_only(self, by_id: dict[str, Task]) -> int | None:
        """`--only ID`: the exit code when that task cannot run now (unknown, or gated by a verifier or
        an approval that is not done and no --force), else None."""
        if not self.only:
            return None
        if self.only not in by_id:
            print(f"no task with id '{self.only}'", flush=True)
            return 2
        gates = [v for v in ancestors(by_id[self.only], by_id)
                 if (v.is_approval or v.is_verifier)
                 and self.state.task(v.id).status != "done"]
        if gates and not self.force:
            print(f"task {self.only} is gated by "
                  + ("approval " if all(v.is_approval for v in gates) else "verifier or approval ")
                  + ", ".join(f"{v.id} ({self.state.task(v.id).status})" for v in gates)
                  + "; run the verifier first, or pass --force to run it anyway", flush=True)
            return 5
        if gates:
            self.log(f"task {self.only}: --force past gate {', '.join(v.id for v in gates)}")
        return None

    def _log_run_start(self, ordered: list[Task]) -> None:
        if self.state.recovered_from:
            self.log(f"WARNING state/run.json was damaged; the state comes from {self.state.recovered_from} "
                     "(the previous save). Tasks that finished after that save are rerun or recovered from git.")
        self.log(f"run {self.run_id} start: {len(ordered)} tasks, providers "
                 f"{' > '.join(self.sched.providers)}, current {self.sched.current}")
        if self.state.current and self.state.current != self.sched.current and mid_attempt(self.state):
            self.log(f"state says current = {self.state.current} with task(s) "
                     f"{', '.join(mid_attempt(self.state))} mid-attempt: beginning from {self.sched.current}"
                     + (" (--provider)" if self.start != self.cfg.start_provider else " (start_provider)"))

    def _recover_previous_run(self, ordered: list[Task]) -> set[str]:
        """Take care of what a run that did not end cleanly left, and apply the decisions made meanwhile.
        Returns the checkpoints decided."""
        self.probe_sandbox()
        midway = [tid for tid, s in self.state.tasks.items() if s.status == "running"]
        if midway:
            self.notify("run_died", note="the previous run ended without finishing (crash, kill -9 or power "
                                         "loss); tasks left mid-way: " + ", ".join(midway))
        self.end_leftover_agents()
        self.recover(ordered)
        self.state.running = {}         # live work of a run that crashed
        return self.apply_decisions()

    def _pending_tasks(self, ordered: list[Task], by_id: dict[str, Task], decided) -> list[Task]:
        if self.only:
            pending = [by_id[self.only]]
        else:
            # a rejected checkpoint stays rejected until someone approves it
            pending = [t for t in ordered if self.state.task(t.id).status not in FINISHED
                       and not (t.is_approval and self.state.task(t.id).status == "rejected")]
        return [t for t in pending if t.id not in decided]

    def _end_run(self, ordered: list[Task], pending: list[Task]) -> int:
        """After the loop: the reports, and the exit code (0 done or stopped cleanly, 3 stopped)."""
        self._ended = True
        if self.disk_full:
            raise self.disk_full
        self.write_branch_report(ordered)
        self.write_summary(ordered)
        if self.stop_reason:
            return self._end_stopped(ordered, pending)
        self._log_awaiting(ordered, pending)
        self.log("run end")
        self.notify("run_end", note=self.run_summary_line(ordered))
        return 0

    def _end_stopped(self, ordered: list[Task], pending: list[Task]) -> int:
        if self.graceful_stop and not self.stop_requested:
            left = [t.id for t in pending]
            self.log(f"run ended cleanly: {self.stop_reason}"
                     + (f"; {len(left)} task(s) left for the next run" if left else ""))
            self.notify("run_end", note=f"{self.stop_reason}; " + self.run_summary_line(ordered))
            return 0
        self.log(f"run stopped: {self.stop_reason}")
        self.notify("run_stopped", note=self.stop_reason)
        return 3

    def _log_awaiting(self, ordered: list[Task], pending: list[Task]) -> None:
        """Checkpoints still waiting for a person: say what they hold and how to decide."""
        for t in ordered:
            if self.state.task(t.id).status == "awaiting":
                held = [d.id for d in ordered if t.id in d.depends_on and d.id in {p.id for p in pending}]
                self.log(f"waiting for approval: {t.id}"
                         + (f" (holds {', '.join(held)})" if held else "")
                         + f"; read results/{t.id}.md, then `baton approve {t.id}` "
                           f"or `baton reject {t.id} --note ...` and run baton again")

    def run_summary_line(self, ordered: list[Task]) -> str:
        counts: dict[str, int] = {}
        for t in ordered:
            st = self.state.task(t.id).status
            counts[st] = counts.get(st, 0) + 1
        return f"{len(ordered)} tasks: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))

    def notify(self, event: str, task: str = "", status: str = "", note: str = "") -> None:
        """Tell the `on_event` hook, if there is one. Never raises, never waits for the hook."""
        try:
            self.notifier.emit(event, task, status, note)
        except Exception as e:         # nothing about a notification may end a run
            self.log(f"on_event {event}: {e}")

    def notify_sleep(self, until: datetime, now: datetime, why: str, task: str = "") -> None:
        if self.notifier.enabled and until - now >= timedelta(minutes=self.cfg.on_event_sleep_min):
            self.notify("sleep", task, note=f"sleeping until {self.fmt(until)} ({why})")

    def serial_loop(self, ordered: list[Task], by_id: dict[str, Task], pending: list[Task],
                    seen: set[str]) -> tuple[list[Task], dict[str, Task]]:
        """One task at a time: the loop that has always been, used when lanes = 1."""
        idle = False
        while (pending or self.follow) and not self.stop_reason:
            if self.stop_wanted():
                break
            self.drop_decided(pending)
            if self.follow:
                # edits to pending tasks' dependencies count at once, not only when the queue is empty
                ordered, by_id, new = self.reload(ordered, by_id, seen)
                pending += [t for t in new if t not in pending]
            ready = self.ready_tasks(pending, by_id)
            if not ready:
                if not self.follow:
                    break
                go_on, idle = self.wait_for_new_work(idle)
                if not go_on:
                    break
                ordered, by_id, new = self.reload(ordered, by_id, seen)
                pending += new
                continue
            idle = False
            task = self._next_task(ready, by_id)
            if not self._run_one(task):
                break
            if self.state.task(task.id).status != "waiting":
                pending.remove(task)
            self.write_summary(ordered)
        return ordered, by_id

    def drop_decided(self, pending: list[Task]) -> None:
        """Apply the approvals and rejections made meanwhile; the checkpoints decided leave `pending`
        (in place: the caller reads the list afterwards)."""
        decided = self.apply_decisions()
        pending[:] = [t for t in pending if t.id not in decided]
        self.take_freed(pending)

    def wait_for_new_work(self, idle: bool) -> tuple[bool, bool]:
        """--follow with nothing to run: say so once, then wait one poll interval. Returns (go on, idle):
        go on is False when a stop was asked for during the wait."""
        if not idle:
            idle = True
            self.log("nothing to run; waiting for new tasks and approvals "
                     "(`baton stop` or Ctrl+C ends the run)")
        self.refresh("idle, waiting for new tasks and approvals")
        until = self.clock.now() + timedelta(seconds=self.cfg.follow_poll_s)
        return self.clock.sleep_until(until, self.stop_wanted), idle

    def _next_task(self, ready: list[Task], by_id: dict[str, Task]) -> Task:
        """Skip-ahead: the first ready task that can start now, else the first ready task (it sleeps
        until its providers reset)."""
        tid = self.sched.select([(t.id, self.needs(t)) for t in ready], self.clock.now())
        task = by_id[tid] if tid else ready[0]
        self.state.task(task.id).gated_by = None
        self._others = [t for t in ready if t is not task]
        return task

    def _run_one(self, task: Task) -> bool:
        """Run one task of the serial loop. False when the disk is full and the loop must stop at once."""
        try:
            self.run_task(task)
        except RunStopped as e:
            self.stop_reason = self.stop_reason or str(e)
        except DiskFull as e:       # nothing can be saved: stop at once, state stays as last saved
            self.disk_full = self.disk_full or e
            return False
        except Exception as e:      # a bug: say so in run.log, end this task, go on with the others
            self.fail_internal(task, e)
        self.activity = None
        return True

    def fail_internal(self, task: Task, e: Exception) -> None:
        """An unexpected exception inside run_task: write the traceback to run.log and leave the task
        `failed` with the reason, so state, SUMMARY.md and the rest of the queue stay consistent."""
        self.log(f"task {task.id}: internal error\n{traceback.format_exc()}")
        note = f"internal error: {type(e).__name__}: {e}"
        try:
            self.finish(task, "failed", note)
        except Exception:               # finish() was what failed: set the state by hand
            ts = self.state.task(task.id)
            ts.status, ts.note, ts.finished = "failed", note, self.clock.now().isoformat()
        self.save()

    def probe_sandbox(self) -> None:
        """Take Codex out of this run when its sandbox cannot start, before it
        can mark tasks blocked for something that is not their fault."""
        pc = self.cfg.providers.get("codex")
        if "codex" not in self.sched.providers or pc is None:
            return
        ok, detail = sandbox.probe(pc)
        if ok is False:
            self.disable_codex_sandbox(detail)
        elif ok is None:
            self.log(f"codex sandbox not checked: {detail}")

    def disable_codex_sandbox(self, detail: str) -> None:
        self.sched.disable("codex", f"sandbox unusable: {detail}")
        self.log(f"codex left out of this run: its sandbox cannot start ({detail}). {sandbox.HINT}")
        self.save()

    def run_task(self, task: Task, provider: str | None = None) -> None:
        """Run one task to its end. With lanes, `provider` is what the dispatcher assigned: the task
        stays on it for its retries and goes back to the dispatcher when that provider is limited."""
        self.state.task(task.id)
        if task.is_approval:
            self.run_approval(task)
            return
        self.require_services(task)
        if task.template:
            self.run_check(task)
            return
        loop = self._start_task(task)
        if loop is None:
            return
        loop.fixed = provider
        while True:
            if self.stop_requested:
                raise RunStopped(self.stop_reason or "stop requested")
            step, pick = self._choose_provider(task, loop, self.clock.now())
            if step == _DONE:
                return
            if step == _REQUEUE:
                self._requeue(task, pick, loop)
                return
            if step == _AGAIN:
                continue
            tried = self._run_attempt(task, loop, pick)
            after = self._after_attempt(task, loop, tried)
            if after == _DONE:
                return
            if after == _REQUEUE:
                self._requeue(task, f"{tried.prov} unavailable", loop)
                return

    def _requeue(self, task: Task, why: str | None, loop: _TaskLoop | None = None) -> None:
        """Lanes: the provider is limited or gone. The task keeps its attempts and its branch and goes
        back to the ready tasks; the dispatcher picks where it continues, as the switch inside the loop does."""
        ts = self.state.task(task.id)
        if loop is not None:        # the retries already used stay used: the next run_task call picks them up
            self._budgets[task.id] = (loop.no_result_left, loop.transient_left, loop.keys_left, loop.held_status,
                                      loop.stall_left)
        ts.status = "waiting"
        ts.note = f"back in the queue: {why or 'provider unavailable'}"
        self.refresh(f"back in the queue ({why or 'provider unavailable'})")

    def _start_task(self, task: Task) -> _TaskLoop | None:
        """Check out the branch and set up the task's state; None if the repository cannot be prepared."""
        ts = self.state.task(task.id)
        missing = missing_for(self.cfg, task)
        if missing:
            # nothing of this task has started: stop before an attempt (and quota) is spent on skipped tests
            raise RunStopped(explain(task, missing))
        if not self._prepare_branch(task, ts):
            return None
        new_start = ts.status not in ("running", "waiting") or ts.start_head is None
        if new_start:
            self._reset_for_new_start(task, ts)
        elif task.is_verifier:
            self._rebase_resumed_verifier(task, ts)
        ts.status = "running"
        self.refresh("starting")
        self.log(f"task {task.id}: start ({task.repo.name}, rung {task.resolved_rung})")
        return self._new_loop(task, ts, new_start)

    def _prepare_branch(self, task: Task, ts) -> bool:
        """Put the repository on the task's branch. False (the task failed) when it cannot be done."""
        try:
            if task.branch:
                self.note_initial_branch(task.repo)
                if not (ts.status in ("running", "waiting") and ts.start_head is not None):
                    self.check_existing_branch(task)       # not for a task that continues its own branch
                # no `branch` in the task file: baton/<id> starts from whatever is checked out now
                msg = gitutil.checkout(task.repo, task.branch, task.base or ("HEAD" if task.branch_auto else None))
                if msg:
                    self.log(f"task {task.id}: {msg}")
        except gitutil.GitError as e:
            earlier = self.commit_failed.get(str(task.repo))
            # nothing of this task happened: its repository may hold someone else's changes on someone
            # else's branch, so no commit and no commit count
            self.finish(task, "failed", f"cannot prepare repository: {e}"
                        + (f" (the WIP commit that should have cleaned it up failed: {earlier})" if earlier else ""),
                        started=False)
            return False
        return True

    def _reset_for_new_start(self, task: Task, ts) -> None:
        """A task that starts (or starts again after it ended) begins with clean counters and notes."""
        if ts.attempts:
            # a task run again after it ended: its counters and notes start over, and the old
            # attempt files move aside instead of being overwritten by attempt 1
            self.archive_attempts(task)
            ts.attempts = 0
        ts.last_end = ""
        ts.reason = ""
        ts.suspect_files = []
        self.progress_path(task).unlink(missing_ok=True)
        ts.start_head = gitutil.head(task.repo)
        ts.started = self.clock.now().isoformat()
        ts.providers = []

    def _rebase_resumed_verifier(self, task: Task, ts) -> None:
        """A verifier resumed after an interruption keeps its `start_head`, so the test-path check would
        count the source commits that other tasks made on the branch meanwhile as its own. When another
        task of this repository ended after the verifier started, the check begins at the current head."""
        started = ts.started or ""
        others = [t for t in self.ordered if t.id != task.id and t.runs_agent and t.repo_key == task.repo_key
                  and (self.state.task(t.id).finished or "") > started]
        head = gitutil.head(task.repo) if others else None
        if head and head != ts.start_head:
            self.log(f"task {task.id}: resumed after {', '.join(t.id for t in others)} committed on the branch; "
                     "its test-path check starts from the current head")
            ts.start_head = head

    def _new_loop(self, task: Task, ts, new_start: bool) -> _TaskLoop:
        """The retry budgets and flags of this run of the task (lanes: what an earlier trip already used)."""
        cfg = self.cfg
        rc = cfg.repos.get(task.repo)
        check_keys = (cfg.hardcoded_key_check or (rc and rc.forbid_literals)) and not task.is_verifier
        loop = _TaskLoop(
            result_path=cfg.results_dir / f"{task.id}.md",
            branch_at_start=gitutil.current_branch(task.repo),
            no_result_left=cfg.no_result_retries,
            transient_left=cfg.max_transient,
            keys_left=1 if check_keys else 0,
            # a task that has not started an attempt may return to the preferred provider;
            # once it has, it stays where it is until that provider is limited
            fresh=new_start or ts.attempts == 0,
        )
        used = self._budgets.pop(task.id, None)
        if used and not new_start:      # lanes: the task was given back to the dispatcher and is back
            (loop.no_result_left, loop.transient_left, loop.keys_left, loop.held_status,
             loop.stall_left) = used
        return loop

    def _choose_provider(self, task: Task, loop: _TaskLoop, now: datetime):
        """Pick the provider for the next attempt, or wait/stop. Returns (_GO, pick), (_AGAIN, None)
        after a sleep, or (_DONE, None) when the task ended or stepped aside."""
        self.reload_for_attempt(task)       # a front-matter or baton.toml edit made meanwhile applies now
        allowed = task.providers
        if loop.avoid:                  # env-limited: the one retry goes to another provider
            allowed = [p for p in (task.providers or self.sched.providers) if p != loop.avoid]
        if loop.fixed:                  # lanes: the dispatcher chose; asking the scheduler would change its state
            return self._assigned_provider(task, loop, now)
        pick = self.sched.pick(now, allowed, fresh=loop.fresh, models=self.task_models(task))
        if pick.provider is None:
            return self._no_provider(task, allowed, pick)
        if pick.wait_until is not None:
            return self._wait_for_pick(task, allowed, pick, now)
        return self._go_with(task, pick)

    def _no_provider(self, task: Task, allowed, pick):
        """No provider can ever run the task: block it (its own list is never extended), or stop the run."""
        if allowed:
            # never fall back to a provider outside the task's list
            label = "verifier" if task.is_verifier else "allowed"
            self.finish(task, "blocked", f"no {label} provider can run: {pick.reason}")
            return _DONE, None
        self.save()
        raise RunStopped(pick.reason)

    def _wait_for_pick(self, task: Task, allowed, pick, now: datetime):
        """Every provider the task may use is limited: run another task meanwhile, block the task when the
        reset is too far away, or sleep until it."""
        cfg = self.cfg
        label = "verifier" if task.is_verifier else "allowed"
        other = self.skip_ahead(task, now)
        if other:
            ts = self.state.task(task.id)
            ts.status = "waiting"
            ts.note = (f"waiting for {', '.join(allowed or self.sched.providers)} "
                       f"(limited until {self.fmt(pick.wait_until)})")
            self.save()
            self.log(f"task {task.id}: {', '.join(allowed or self.sched.providers)} limited until "
                     f"{self.fmt(pick.wait_until)}; running {other.id} meanwhile")
            return _DONE, None
        wait = pick.wait_until - now
        if wait > timedelta(hours=cfg.max_sleep_hours) and allowed:
            self.finish(task, "blocked", f"{label} providers ({', '.join(allowed)}) are limited "
                                         f"until {self.fmt(pick.wait_until)}, more than "
                                         f"{cfg.max_sleep_hours:g}h away")
            return _DONE, None
        if wait > timedelta(hours=cfg.max_sleep_hours):
            self.save()
            raise RunStopped(f"every provider is limited and the earliest reset "
                             f"({pick.provider}, {self.fmt(pick.wait_until)}) is more than "
                             f"{cfg.max_sleep_hours:g}h away")
        self.refresh(f"sleeping until {self.fmt(pick.wait_until)} ({pick.reason})")
        self.notify_sleep(pick.wait_until, now, pick.reason, task.id)
        self.log(f"task {task.id}: {self.limited_wording(allowed)}, sleeping until "
                 f"{self.fmt(pick.wait_until)} ({pick.reason})")
        if not self.clock.sleep_until(pick.wait_until, self.stop_wanted):
            raise RunStopped(self.stop_reason or "stop requested")
        self.log(f"task {task.id}: resuming after reset on {pick.provider}")
        self.refresh(f"resuming on {pick.provider}")
        return _AGAIN, None

    def _go_with(self, task: Task, pick):
        """A provider is available: say how the choice came about and check the rung has a model for it."""
        ts = self.state.task(task.id)
        prov = pick.provider
        if pick.switched_from:
            self.log(f"switch {pick.switched_from} -> {prov} ({pick.reason})")
        elif pick.restricted_from and not ts.attempts:
            self.log(f"{task.id} runs on {prov} ({pick.reason})")
        if self.cfg.routing.spec(task.resolved_rung, prov) is None:   # preflight rejects this; kept as a guard
            self.finish(task, "failed", f"rung {task.resolved_rung} has no {prov} model")
            return _DONE, None
        return _GO, pick

    def _assigned_provider(self, task: Task, loop: _TaskLoop, now: datetime):
        """Lanes: the dispatcher chose the provider. If it has been limited or disabled meanwhile,
        give the task back instead of sleeping: another lane's task may use the free provider first."""
        ts = self.state.task(task.id)
        prov = loop.fixed
        if not self.sched.available(prov, now, self.task_models(task).get(prov)):
            return _REQUEUE, f"{prov} {self.sched._why(prov)}"
        last = ts.providers[-1] if ts.providers else None
        pick = Pick(prov, None)
        if last and last != prov:
            pick.switched_from = last
            pick.reason = self.sched._why(last) if not self.sched.available(last, now) else "dispatched"
            self.log(f"switch {last} -> {prov} ({pick.reason})")
        if self.cfg.routing.spec(task.resolved_rung, prov) is None:   # preflight rejects this; kept as a guard
            self.finish(task, "failed", f"rung {task.resolved_rung} has no {prov} model")
            return _DONE, None
        return _GO, pick

    def _run_attempt(self, task: Task, loop: _TaskLoop, pick) -> _Tried:
        """Run one agent session and read back what it did: result status, outcome, metrics."""
        prov = pick.provider
        spec, n = self._begin_attempt(task, loop, pick)
        att = self._launch(task, loop, prov, spec, n)
        now = self.clock.now()
        status = read_status(loop.result_path, att.wall_start)
        if att.returned_to and not status:
            return self._returned(task, loop, pick, spec, n, att, now)
        oc, status, weak_note = self._read_outcome(task, loop, prov, att, status, now)
        if prov == "codex":
            self.commit_codex_attempt(task, loop.result_path, att.wall_start, _codex_why(att, oc, status), status)
        self._record_metrics(task, loop, pick, spec, n, att, oc, status)
        return _Tried(prov=prov, n=n, att=att, oc=oc, status=status, weak_note=weak_note, now=now)

    def _begin_attempt(self, task: Task, loop: _TaskLoop, pick):
        """Count the attempt, remember the provider, say what is about to run. Returns (model spec, number)."""
        cfg = self.cfg
        ts = self.state.task(task.id)
        prov = pick.provider
        missing = missing_for(cfg, task, prov)       # a variable only this provider's run lacks
        if missing:
            raise RunStopped(explain(task, missing))
        spec = cfg.routing.spec(task.resolved_rung, prov)
        loop.fresh = False
        ts.attempts += 1
        if not ts.providers or ts.providers[-1] != prov:
            ts.providers.append(prov)
        n = ts.attempts
        self.refresh(f"attempt {n} on {prov} since {self.fmt(self.clock.now())}"
                     + (f" (switched from {pick.switched_from})" if pick.switched_from else ""))
        self.log(f"task {task.id}: attempt {n} on {prov} ({spec.model}"
                 f"{' ' + spec.effort if spec.effort else ''})")
        return spec, n

    def _launch(self, task: Task, loop: _TaskLoop, prov: str, spec, n: int) -> Attempt:
        """Build the prompt and run the agent. A stop that arrived meanwhile keeps the work and ends the run."""
        ts = self.state.task(task.id)
        result_path = loop.result_path
        agent_result = self.agent_result_path(task, prov, result_path)
        if prov == "codex":
            self.clear_codex_files(task)
        prompt = self.build_prompt(task, prov, n, ts.last_end, agent_result)
        self.hook_failed.pop(task.id, None)
        if loop.fixed:
            self.set_running(task, provider=prov, attempt=n,
                             out=str(self.cfg.results_dir / task.id / f"attempt-{n:02d}.{prov}.out"))
        att = self.execute(task, prov, spec, prompt, n, watch=self.return_watch(task, prov, loop))
        if prov == "codex":
            self.promote_codex_files(task, result_path, att.wall_start)
        if self.stop_requested:
            self.commit_leftovers(task, "the run was stopped")
            ts.last_end = "the run was stopped in the middle of the previous attempt"
            self.save()
            raise RunStopped(self.stop_reason or "stop requested")
        return att

    def _read_outcome(self, task: Task, loop: _TaskLoop, prov: str, att: Attempt, status, now: datetime):
        """Classify what the agent printed and settle the result status. Returns (outcome, status, weak note)."""
        cfg = self.cfg
        weak_note = ""
        out_text = read_output(att.out_path)         # bounded: a runaway agent can write gigabytes
        err_text = read_output(att.err_path)
        oc = classify(prov, out_text, err_text, att.exit_code, now, self.tz,
                      home_dir(cfg.providers[prov]),
                      datetime.fromtimestamp(att.wall_start, tz=timezone.utc))
        # baton itself ended a timed-out, stalled or stopped attempt: that is not a changed message format
        warning = (None if att.timed_out or att.stalled or att.returned_to
                   else unmatched_failure_warning(prov, oc, out_text, err_text, att.exit_code))
        if warning:
            self.log(f"task {task.id}: {warning}")
        if (prov == "codex" and not status and att.exit_code == 0
                and not att.timed_out and oc.kind == "success"):
            # no usable result file (missing, stale or without STATUS):
            # take the verdict from the last message of a clean exit only
            status, weak_note = self._codex_last_message_status(task, out_text)
        if not status and loop.held_status and oc.kind == "success" and not att.timed_out:
            status = loop.held_status        # the re-check attempt left the earlier result file standing
        return oc, status, weak_note

    def _codex_last_message_status(self, task: Task, out_text: str):
        """The verdict read from Codex's last message, as weak evidence: no result file, so nothing says
        what was changed or checked. A `done` from it is only a `partial`."""
        status = read_codex_status(out_text)
        if not status:
            return status, ""
        weak_note = "no result file; STATUS taken from Codex's last message"
        if status == "done":
            status, weak_note = "partial", weak_note + " (done downgraded to partial)"
        self.log(f"task {task.id}: {weak_note}")
        return status, weak_note

    def _record_metrics(self, task: Task, loop: _TaskLoop, pick, spec, n: int, att: Attempt, oc, status) -> None:
        self.metrics({
            "run": self.run_id, "task": task.id, "attempt": n, "work_type": task.work_type,
            "rung": task.resolved_rung, "provider": pick.provider, "model": spec.model, "effort": spec.effort,
            "switched_from": pick.switched_from, "started": att.started.isoformat(),
            "seconds": round(att.seconds, 1), "exit": att.exit_code, "timed_out": att.timed_out,
            "result": status, "outcome": oc.kind, "window": oc.window,
            "reset_at": oc.reset_at.isoformat() if oc.reset_at else None, "source": oc.source,
            "session": oc.session_id, "usage": oc.usage,
            **({"lane": self.lane} if loop.fixed else {}),
        })

    def _returned(self, task: Task, loop: _TaskLoop, pick, spec, n: int, att: Attempt, now: datetime) -> _Tried:
        """An attempt stopped on purpose because start_provider reset: keep its work as WIP and let
        the task continue on start_provider with the handoff notes."""
        prov, back = pick.provider, att.returned_to
        ts = self.state.task(task.id)
        why = f"stopped so the task could go back to {back}"
        if prov == "codex":
            self.commit_codex_attempt(task, loop.result_path, att.wall_start, why, None)
        else:
            self.commit_leftovers(task, why)
        ts.last_end = (f"{prov} was stopped in the middle of the task because {back}, "
                       "the preferred provider, is available again")
        self.metrics({
            "run": self.run_id, "task": task.id, "attempt": n, "work_type": task.work_type,
            "rung": task.resolved_rung, "provider": prov, "model": spec.model, "effort": spec.effort,
            "switched_from": pick.switched_from, "started": att.started.isoformat(),
            "seconds": round(att.seconds, 1), "exit": att.exit_code, "timed_out": False,
            "result": None, "outcome": "returned", "returned_to": back,
            **({"lane": self.lane} if loop.fixed else {}),
        })
        self.sched.current = back
        self.log(f"switch {prov} -> {back} ({back} available again; {task.id} moves back mid-task)")
        self.save()
        return _Tried(prov=prov, n=n, att=att, oc=Outcome("returned"), status=None, weak_note="",
                      now=now, returned=back)

    def return_watch(self, task: Task, prov: str, loop: _TaskLoop | None = None) -> str | None:
        """The preferred provider this attempt should give the task back to once it resets, or
        None. Only a limit whose reset time was read from the provider's message counts: a
        cooldown guess would stop good work for a provider that may still be limited. Not with
        lanes: there the dispatcher, not the attempt, decides where a task runs."""
        first = self.sched.providers[0]
        wanted = self.cfg.return_mid_task if task.return_mid_task is None else task.return_mid_task
        if not wanted or task.is_verifier or prov == first or (loop and loop.fixed):
            return None
        if task.providers is not None and first not in task.providers:
            return None
        s = self.sched.status[first]
        if s.disabled or s.limited_until is None or not s.reset_known:
            return None
        if self.sched.available(first, self.clock.now(), self.task_models(task).get(first)):
            return None
        return first

    def _after_attempt(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """Decide what the attempt means. Returns _AGAIN to try again or _DONE when the task is over."""
        if t.returned:
            return _AGAIN
        self._check_branch_unchanged(task, loop)
        if t.oc.kind == "sandbox" and t.prov == "codex" and t.status != "done":
            return self._on_codex_sandbox(task, loop, t)
        if t.status:
            return self._accept_result(task, loop, t)
        if t.att.stalled:
            return self._on_stall(task, loop, t)
        return self._on_outcome(task, loop, t)

    def _check_branch_unchanged(self, task: Task, loop: _TaskLoop) -> None:
        """An agent that switched branches leaves the repository where the next task cannot start: stop."""
        here = gitutil.current_branch(task.repo)
        if here != loop.branch_at_start:
            if here:
                self.note_stray_branch(task.repo, here)
            self.finish(task, "stopped", f"{task.repo.name} was left on another branch")
            raise RunStopped(f"task {task.id} left {task.repo.name} on "
                             f"'{gitutil.current_branch(task.repo)}' instead of '{loop.branch_at_start}'")

    def _on_codex_sandbox(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """The provider is broken, not the task: no command ran. Retry elsewhere."""
        ts = self.state.task(task.id)
        loop.result_path.unlink(missing_ok=True)
        self.disable_codex_sandbox(t.oc.detail)
        ts.last_end = ("codex could not start its sandbox, so the previous attempt did nothing "
                       f"useful ({t.oc.detail})")
        self.refresh(f"codex sandbox unusable; moving {task.id} to the next provider")
        return _REQUEUE if loop.fixed else _AGAIN

    def _on_outcome(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """No result file: what the provider's outcome means (limit, auth, temporary error, quick exit)."""
        oc, att = t.oc, t.att
        elsewhere = _REQUEUE if loop.fixed else _AGAIN
        if oc.kind == "limit":
            return _DONE if self._on_limit(task, t) == _DONE else elsewhere
        if oc.kind == "auth":
            self._on_auth(task, t)
            return elsewhere
        if oc.kind == "transient" and loop.transient_left > 0:
            self._on_transient(task, loop, t)
            return _AGAIN
        clean_exit = att.exit_code == 0 and oc.kind == "success"
        if att.seconds < self.cfg.quick_exit_s and not att.timed_out and not clean_exit:
            return self._on_quick_exit(task, t)
        # a clean exit that wrote no result (the agent only said it could not) is not an unknown
        # failure: no long wait, the no-result retry budget decides
        self._reset_quick(task)
        return self._on_no_result(task, loop, t)

    def _on_auth(self, task: Task, t: _Tried) -> None:
        self.sched.disable(t.prov, "authentication error")
        self.commit_leftovers(task, f"{t.prov} authentication error")
        self.log(f"task {task.id}: {t.prov} authentication error, provider disabled for this run: {t.oc.detail}")

    def _on_transient(self, task: Task, loop: _TaskLoop, t: _Tried) -> None:
        loop.transient_left -= 1
        wait = t.oc.retry_after_s or self.cfg.transient_wait_s
        self.log(f"task {task.id}: temporary error on {t.prov}, retrying in {wait:.0f}s: {t.oc.detail}")
        self.refresh(f"temporary error on {t.prov}, retrying in {wait:.0f}s")
        self.pause(wait)

    @staticmethod
    def _result_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _accept_result(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """The agent wrote a STATUS: finish the task, or send it back once for hardcoded keys."""
        ts = self.state.task(task.id)
        status, prov = t.status, t.prov
        self._reset_quick(task)
        status, note = self._apply_hook_failure(task, status, t.weak_note)
        again = self._env_limited(task, loop, t, status)
        if again:
            return again
        if status == "blocked" and ts.reason != "env":
            ts.reason = "finding"
        elif status != "blocked" and ts.reason != "env":
            ts.reason = ""
        status, note = self._apply_skipped_for_env(task, loop, ts, status, note)
        found = self.hardcoded_keys(task, loop.result_path) if status in ("done", "partial") else []
        if found and loop.keys_left > 0:
            return self._ask_for_key_fix(task, loop, prov, status, found)
        if found:
            note = ((note + "; " if note else "")
                    + f"{len(found)} possible hardcoded data key(s), see "
                      f"results/{task.id}/hardcoded-keys.md")
        if ts.reason == "env" and status in ("partial", "blocked") and loop.env_note:
            note = loop.env_note + ("; " + note if note else "")
            loop.env_note = ""
        self.finish(task, status, note, prov=prov, attempt=t.n)
        return _DONE

    def _apply_hook_failure(self, task: Task, status: str, note: str):
        """A commit hook that refused the agent's commit: the work is kept as a WIP commit, not done."""
        if task.id not in self.hook_failed:
            return status, note
        hook = self.hook_failed.pop(task.id)
        if status == "done":
            status = "partial"
        return status, (note + "; " if note else "") + f"commit hook failed, work kept as a WIP commit: {hook}"

    def _apply_skipped_for_env(self, task: Task, loop: _TaskLoop, ts, status: str, note: str):
        """Tests that skip for a missing variable prove nothing: a verifier cannot be done on them, and
        any task that reports them is blocked by the environment, not the code."""
        if not (task.is_verifier or status in ("partial", "blocked")):
            return status, note
        skipped = skipped_for_env(self._result_text(loop.result_path))
        if not skipped:
            return status, note
        if status == "done":
            status = "blocked"
        ts.reason = "env"
        return status, (note + "; " if note else "") + f"blocked:env, tests were skipped: {skipped}"

    def _ask_for_key_fix(self, task: Task, loop: _TaskLoop, prov: str, status: str, found: list) -> str:
        """Send the task back once: possible hardcoded data keys must be resolved or justified."""
        ts = self.state.task(task.id)
        loop.keys_left -= 1
        loop.held_status = status
        ts.last_end = _keys_message(found)
        self.refresh(f"{len(found)} possible hardcoded data key(s); asking {prov} to resolve or justify")
        self.log(f"task {task.id}: {len(found)} possible hardcoded data key(s); "
                 "asking for them to be resolved or justified")
        return _AGAIN

    def _on_stall(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """The agent printed nothing for [run] stall_minutes: it is hung (a test waiting on a service, a
        network call). End the attempt, keep its work as WIP, and try once more in a new session."""
        att, ts = t.att, self.state.task(task.id)
        mins = task.stall_minutes if task.stall_minutes is not None else self.cfg.stall_minutes
        where = att.stall_note or "(no output at all)"
        self.commit_leftovers(task, "the attempt stalled")
        if loop.stall_left > 0:
            loop.stall_left -= 1
            ts.last_end = f"the previous attempt stalled (no output for {mins:g} min) at: {where}"
            self.log(f"task {task.id}: attempt {t.n} stalled (no output for {mins:g} min; last: {where}); "
                     "continuing in a new session")
            self.save()
            return _AGAIN
        self.finish(task, "failed", f"attempt {t.n} stalled again (no output for {mins:g} min; last: {where})",
                    prov=t.prov, attempt=t.n)
        return _DONE

    def _env_limited(self, task: Task, loop: _TaskLoop, t: _Tried, status: str) -> str | None:
        """A partial/blocked verdict that comes from the environment (a sandbox that forbids sockets, a
        service that is down) is not a code failure: say so. With `env_limited_action =
        "retry_other_provider"` and a sandbox restriction, give the task one more attempt on the other
        provider. Returns _AGAIN for that retry, else None."""
        ts = self.state.task(task.id)
        ts.reason = ""
        loop.env_note = ""
        if status not in ("partial", "blocked"):
            return None
        sandbox_side, why = self._env_reason(task, loop, t)
        if not why:
            return None
        ts.reason = "env"
        loop.env_note = f"env-limited: {why}"
        self.log(f"task {task.id}: {status} is environment-limited, not a code failure: {why}")
        if not (sandbox_side and self.cfg.env_limited_action == "retry_other_provider"
                and not loop.fixed and not loop.env_retried):
            return None
        return self._retry_elsewhere(task, loop, t, status, sandbox_side)

    def _env_reason(self, task: Task, loop: _TaskLoop, t: _Tried):
        """Why the verdict looks environmental: (the sandbox restriction seen or None, the reason or None)."""
        sandbox_side = envlimit.detect(_tail_text(t.att.out_path), _tail_text(t.att.err_path),
                                       _tail_text(loop.result_path))
        if sandbox_side:
            return sandbox_side, f"{t.prov}: {sandbox_side}"
        for name in services.needed(self.cfg, task):
            ok, detail = services.check(self.cfg.services[name])
            if not ok:
                return None, f"service '{name}' is down ({detail})"
        return None, None

    def _retry_elsewhere(self, task: Task, loop: _TaskLoop, t: _Tried, status: str, sandbox_side: str) -> str | None:
        """One more attempt on another provider whose sandbox may allow what this one forbade."""
        ts = self.state.task(task.id)
        now = self.clock.now()
        others = [p for p in (task.providers or self.sched.providers) if p != t.prov
                  and self.sched.available(p, now, self.task_models(task).get(p))
                  and self.cfg.routing.spec(task.resolved_rung, p) is not None]
        if not others:
            return None
        loop.env_retried, loop.avoid = True, t.prov
        loop.result_path.unlink(missing_ok=True)
        ts.reason = ""
        self.commit_leftovers(task, f"{t.prov} was limited by its sandbox")
        ts.last_end = (f"{t.prov} ran this task in a sandbox that blocked part of its checks ({sandbox_side}); "
                       f"its verdict ({status}) came from that limit, not from the code. Run the checks "
                       "that could not run before, and report only what they show")
        self.refresh(f"{t.prov} was env-limited; retrying {task.id} on {others[0]}")
        self.log(f"task {task.id}: retrying once on {others[0]} (env_limited_action = retry_other_provider)")
        return _AGAIN

    def require_services(self, task: Task) -> None:
        """Check the services the task needs before it starts; restart a failing one once. If one stays
        down, stop the run with a clear message instead of burning an attempt against it."""
        names = services.needed(self.cfg, task)
        if not names:
            return
        wait = lambda s: self.clock.sleep_until(self.clock.now() + timedelta(seconds=s), self.stop_wanted)  # noqa: E731
        down = services.ensure(self.cfg, names, lambda m: self.log(f"task {task.id}: {m}"), sleep=wait)
        if not down:
            return
        msg = services.describe(down)
        ts = self.state.task(task.id)
        ts.note = f"waiting for the environment: {msg}"
        self.log(f"task {task.id}: {msg}; the run stops here so no attempt is spent on it")
        self.save()
        raise RunStopped(f"{msg}. Fix it and run baton again; task {task.id} has not started")

    def _on_limit(self, task: Task, t: _Tried) -> str | None:
        oc, prov = t.oc, t.prov
        until, how = self.reset_time(oc, t.now)
        model = self.task_models(task).get(prov, "")
        if oc.window == "model" and oc.model and oc.model in model:
            # only this model family is limited: tasks on another model of the provider go on
            self.sched.mark_model_limited(prov, oc.model, until)
        else:
            self.sched.mark_limited(prov, until, oc.window, reset_known=how.startswith("reset time from"))
        self._reset_quick(task)
        self.commit_leftovers(task, f"{prov} hit its usage limit")
        ts = self.state.task(task.id)
        ts.last_end = f"{prov} hit its usage limit in the middle of the task"
        self.refresh(f"{prov} hit its usage limit")
        self.log(f"task {task.id}: {prov} usage limit ({oc.window}), available again "
                 f"{self.fmt(until)} [{how}; {oc.source}]")
        # A limit that keeps coming back while nothing changes in the repository is probably not a real
        # one (a message misread as a limit, or a limit that does not lift): stop bouncing between providers.
        head = gitutil.head(task.repo) if gitutil.is_repo(task.repo) else None
        if head is not None and head == (ts.limit_head or ts.start_head):
            ts.limit_cycles += 1
        else:
            ts.limit_cycles = 1
        ts.limit_head = head
        if ts.limit_cycles >= self.cfg.max_limit_cycles:
            self.finish(task, "failed",
                        f"{ts.limit_cycles} usage-limit outcomes in a row with no new commits or changes "
                        f"(last: {prov}, {oc.source}: {oc.detail}); a limit that never lifts or a message "
                        f"misread as one. See {t.att.out_path}", prov=prov, attempt=t.n)
            return _DONE
        return None

    def _reset_quick(self, task: Task) -> None:
        """Something other than a quick unexplained exit happened: the streaks start over."""
        self.state.quick_fails = 0
        self.state.quick_tasks = []
        self.state.task(task.id).quick_fails = 0

    def _on_quick_exit(self, task: Task, t: _Tried) -> str:
        """An attempt that ended fast for no known reason: wait and retry. The task is `failed` after
        max_quick_unknown such exits of its own and the run goes on with the independent ones; the
        emergency brake (the whole run stops) is for a shared cause: the program is missing or not
        executable, or max_quick_unknown different tasks in a row failed this way."""
        cfg, att, prov = self.cfg, t.att, t.prov
        ts = self.state.task(task.id)
        self.state.quick_fails += 1
        ts.quick_fails += 1
        if task.id not in self.state.quick_tasks:
            self.state.quick_tasks.append(task.id)
        self.save_fixture(task, prov, t.n, att)
        where = f"See {att.out_path} and {att.err_path}"
        if att.exit_code in (126, 127) or len(self.state.quick_tasks) >= cfg.max_quick_unknown:
            why = (f"{prov} could not be started (exit {att.exit_code})" if att.exit_code in (126, 127)
                   else f"{len(self.state.quick_tasks)} different tasks in a row ended fast with no known reason")
            self.finish(task, "stopped", "emergency brake")
            raise EmergencyBrake(f"emergency brake: {why} (last: {task.id} on {prov}, exit {att.exit_code} "
                                 f"after {att.seconds:.0f}s). {where}")
        if ts.quick_fails >= cfg.max_quick_unknown:
            self.finish(task, "failed",
                        f"{ts.quick_fails} quick exits in a row with no known reason (last: {prov} exit "
                        f"{att.exit_code} after {att.seconds:.0f}s); the other tasks go on. {where}",
                        prov=prov, attempt=t.n)
            return _DONE
        self.refresh(f"{prov} exited for an unknown reason, retrying in {cfg.transient_wait_s:.0f}s")
        self.log(f"task {task.id}: {prov} exited after {att.seconds:.0f}s (exit {att.exit_code}) "
                 f"for an unknown reason; waiting {cfg.transient_wait_s:.0f}s")
        self.pause(cfg.transient_wait_s)
        return _AGAIN

    def _on_no_result(self, task: Task, loop: _TaskLoop, t: _Tried) -> str:
        """No result file and nothing recognisable: retry in a new session a few times, then fail."""
        att = t.att
        why = "timed out" if att.timed_out else f"ended without a result file (exit {att.exit_code})"
        self.commit_leftovers(task, why)
        if loop.no_result_left > 0:
            loop.no_result_left -= 1
            self.state.task(task.id).last_end = f"the previous attempt {why}"
            self.save()
            self.log(f"task {task.id}: attempt {t.n} {why}; continuing in a new session")
            return _AGAIN
        self.finish(task, "failed", f"attempt {t.n} {why}", prov=t.prov, attempt=t.n)
        return _DONE

    def limited_wording(self, allowed: list[str] | None) -> str:
        """Say which providers the wait is for, and which ones the task may not use."""
        barred = [p for p in self.sched.providers if allowed and p not in allowed]
        if not barred:
            return "all providers limited"
        ok = [p for p in self.sched.providers if p in allowed]
        return (f"allowed providers ({', '.join(ok)}) limited; {', '.join(barred)} "
                f"not allowed for this task")

    def task_models(self, task: Task) -> dict[str, str]:
        """provider -> the model id (lower case) this task's rung uses there."""
        out = {}
        for p in self.sched.providers:
            spec = self.cfg.routing.spec(task.resolved_rung, p) if task.runs_agent else None
            if spec is not None and spec.model:
                out[p] = spec.model.lower()
        return out

    def skip_ahead(self, task: Task, now: datetime) -> Task | None:
        """Another ready task that can start now, if this one may step aside:
        its repository must be clean, so the other task can check out its branch."""
        tid = self.sched.select([(t.id, self.needs(t), self.task_models(t)) for t in self._others], now)
        if tid is None or self.stop_requested:
            return None
        if gitutil.is_repo(task.repo) and gitutil.is_dirty(task.repo):
            return None
        return next(t for t in self._others if t.id == tid)

    def pause(self, seconds: float) -> None:
        until = self.clock.now() + timedelta(seconds=seconds)
        if not self.clock.sleep_until(until, self.stop_wanted):
            raise RunStopped(self.stop_reason or "stop requested")

    def reset_time(self, oc: Outcome, now: datetime) -> tuple[datetime, str]:
        margin = timedelta(seconds=self.cfg.reset_margin_s)
        if oc.reset_at and now - timedelta(minutes=5) <= oc.reset_at <= now + timedelta(days=8):
            return max(oc.reset_at, now) + margin, "reset time from the message"
        return now + timedelta(minutes=self.cfg.unknown_limit_cooldown_min), "reset time unknown, cooldown"

    def finish(self, task: Task, status: str, note: str, prov: str | None = None, attempt: int | None = None,
               started: bool = True) -> None:
        """Record how a task ended: commit what is left, decide the final status (see baton.finishing),
        save, log and notify."""
        ts = self.state.task(task.id)
        facts = self._finish_facts(task, ts, started)
        status, note = decide_finish(status, note, facts)
        ts.status = status
        ts.note = note
        ts.finished = self.clock.now().isoformat()
        self._warn_dirty_watch_repos(task, ts)
        self.save()
        self.write_status_file(task)
        self.log(f"task {task.id}: {status} ({ts.commits} commits, "
                 f"{' -> '.join(ts.providers) or '-'}){': ' + ts.note if ts.note else ''}")
        self.release_dependents(task)
        if status in TASK_EVENTS:
            self.notify(TASK_EVENTS[status], task.id, self.status_label(ts), ts.note)

    def _finish_facts(self, task: Task, ts, started: bool) -> FinishFacts:
        """What the repository says at the end of a task: commit the leftovers and count the commits (a
        task that never started has none, and touches nothing), and list what a verifier changed."""
        facts = FinishFacts(runs_agent=task.runs_agent, is_verifier=task.is_verifier)
        if not started:
            ts.commits = 0
        elif task.runs_agent and gitutil.is_repo(task.repo):
            facts.left = self.commit_leftovers(task, "left uncommitted at the end of the task")
            ts.commits = gitutil.count_commits(task.repo, ts.start_head) if ts.start_head else 0
        facts.commits = ts.commits
        facts.refused = self.refused.pop(task.id, [])
        facts.suspect = list(ts.suspect_files)
        if task.is_verifier and ts.start_head and gitutil.is_repo(task.repo):
            facts.stray = outside_test_paths(gitutil.changed_paths(task.repo, ts.start_head),
                                             self.cfg.verifier_test_paths)
        return facts

    def _warn_dirty_watch_repos(self, task: Task, ts) -> None:
        """Say so when the task left changes in a repository it should not have touched. With lanes, a
        repository another lane is working in is dirty by design."""
        dirty = [r.name for r in self.cfg.watch_repos
                 if task.runs_agent and r.resolve() != task.repo and gitutil.is_repo(r)
                 and not self.repo_busy(r) and gitutil.is_dirty(r)]
        if dirty:
            ts.note = (ts.note + "; " if ts.note else "") + "uncommitted changes in " + ", ".join(dirty)
            self.log(f"task {task.id}: WARNING uncommitted changes in {', '.join(dirty)}")

    # ------------------------------------------------------------ prompt and process

    # ------------------------------------------------------------ summary

