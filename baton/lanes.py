"""Several tasks at once ([run] lanes > 1): the dispatcher and its workers.

One dispatcher (the thread that called `Runner.run`) owns the queue, the
provider status and the lane table. It starts one worker thread per task, in
different repositories, and takes them back when they end. A worker is today's
`run_task` for one task. Workers write the shared files (run.json, SUMMARY.md,
run.log, metrics.jsonl) only through the runner's lock, one whole write at a
time. With `lanes = 1` none of this runs: `Runner.serial_loop` is used.

See docs/design/parallel-lanes.md.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import gitutil
from .process import EmergencyBrake, RunStopped
from .state import DiskFull
from .scheduler import Candidate
from .tasks import Task

LANE_POLL_S = 0.2           # how often the dispatcher looks again while workers run


@dataclass
class _Worker:
    task: Task
    provider: str | None
    thread: threading.Thread


@dataclass
class _Dispatch:
    """What the dispatcher carries from one turn to the next."""
    ordered: list[Task]
    by_id: dict[str, Task]
    pending: list[Task]
    seen: set[str]
    slots: dict[str, int]
    running: dict[str, _Worker] = field(default_factory=dict)
    events: queue.Queue = field(default_factory=queue.Queue)
    idle: bool = False
    timeout: float = 0.0
    next_reload: float = 0.0


class LanesMixin:
    """Methods of baton.runner.Runner, split out of it; they use its attributes."""

    # ------------------------------------------------------------ shared bits

    def repo_busy(self, repo: Path) -> bool:
        """True when a worker is inside this repository right now."""
        return bool(self._busy_repos) and str(gitutil.toplevel(Path(repo))) in self._busy_repos

    def set_running(self, task: Task, **fields) -> None:
        """Record what a lane is doing, for `baton status` and `baton watch`."""
        with self._lock:
            self.state.running.setdefault(task.id, {}).update(fields)
            self.save()

    def halt_lanes(self, reason: str) -> None:
        """Stop every lane: the emergency brake, or a crash. Each worker commits its leftovers."""
        with self._lock:
            self.stop_reason = self.stop_reason or reason
            self.stop_requested = True
        self.kill_children()

    def last_provider(self, task: Task) -> str | None:
        """Where the task continues if it can: the provider it last ran on. A task that has not started
        goes to the preferred provider, or stays on the last one used (return_to_start_provider = false)."""
        ts = self.state.task(task.id)
        if ts.attempts > 0 and ts.providers:
            return ts.providers[-1]
        return None if self.cfg.return_to_start_provider else self.sched.current

    # ------------------------------------------------------------ dispatcher

    def dispatch(self, ordered: list[Task], by_id: dict[str, Task], pending: list[Task],
                 seen: set[str]) -> tuple[list[Task], dict[str, Task]]:
        cfg = self.cfg
        for t in ordered:
            self.state.task(t.id)       # no new dict keys while a worker thread saves the state
        slots = {p: cfg.providers[p].max_concurrent for p in self.sched.providers}
        st = _Dispatch(ordered, by_id, pending, seen, slots)
        self.log(f"lanes: up to {cfg.lanes} tasks at once; slots "
                 + ", ".join(f"{p} x{n}" for p, n in slots.items()))
        try:
            while self._tick(st):
                pass
        except BaseException:
            self.halt_lanes("the dispatcher failed")
            for w in st.running.values():
                w.thread.join()
            raise
        if self._crash is not None:
            raise self._crash
        return st.ordered, st.by_id

    def _tick(self, st: "_Dispatch") -> bool:
        """One turn of the dispatcher: take back the workers that ended, start what can start, and say
        how long to wait for the next turn. False when the run is over."""
        self._reap(st)
        if self.stop_reason or self.stop_wanted():
            # start nothing new; let every busy lane finish (or, after a signal, clean up)
            if not st.running:
                return False
            st.timeout = LANE_POLL_S
            return True
        self._follow_reload(st)
        self.drop_decided(st.pending)
        if not st.pending and not self.follow and not st.running:
            return False
        ready = [t for t in self.ready_tasks(st.pending, st.by_id) if t.id not in st.running]
        now = self.clock.now()
        started = self._start_ready(ready, st.running, st.slots, st.events, now)
        if st.running:
            st.idle = False
            st.timeout = LANE_POLL_S
            return True
        st.timeout = 0.0
        if started:
            return True
        if not ready:
            return self._idle_turn(st)
        st.idle = False
        return self._wait_for_providers(ready, st.pending, st.slots, now)

    def _reap(self, st: "_Dispatch") -> None:
        """Take back the workers that ended (waiting up to the turn's timeout for the first)."""
        for tid in self._events(st.events, st.timeout):
            w = st.running.pop(tid)
            w.thread.join()
            with self._lock:
                self._busy_repos.discard(w.task.repo_key)
                self.state.running.pop(tid, None)
                self._acts.pop(tid, None)
                if self.state.task(tid).status != "waiting" and w.task in st.pending:
                    st.pending.remove(w.task)
                if self.state.task(tid).status != "waiting":
                    self._budgets.pop(tid, None)
                self.save()
                self.write_summary(st.ordered)

    def _follow_reload(self, st: "_Dispatch") -> None:
        """--follow: look for new task files now and then while workers run."""
        if self.follow and time.monotonic() >= st.next_reload:
            st.next_reload = time.monotonic() + self.cfg.follow_poll_s
            st.ordered, st.by_id = self._reload(st.ordered, st.by_id, st.seen, st.pending)

    def _idle_turn(self, st: "_Dispatch") -> bool:
        """Nothing is ready and nothing runs. Without --follow the run is over; with it, wait for new work."""
        if not self.follow:
            return False
        go_on, st.idle = self.wait_for_new_work(st.idle)
        if not go_on:
            return False
        st.ordered, st.by_id = self._reload(st.ordered, st.by_id, st.seen, st.pending)
        return True

    def _reload(self, ordered, by_id, seen, pending):
        """--follow: pick up new task files. Under the lock: new tasks add keys to the state a worker may be saving."""
        with self._lock:
            ordered, by_id, new = self.reload(ordered, by_id, seen)
            for t in new:
                self.state.task(t.id)
            pending += new
        return ordered, by_id

    @staticmethod
    def _events(events: queue.Queue, timeout: float) -> list[str]:
        """The workers that have ended: wait up to `timeout` for the first, then take what is there."""
        got: list[str] = []
        try:
            got.append(events.get(timeout=timeout) if timeout else events.get_nowait())
        except queue.Empty:
            return got
        while True:
            try:
                got.append(events.get_nowait())
            except queue.Empty:
                return got

    def _start_ready(self, ready: list[Task], running: dict[str, _Worker], slots: dict[str, int],
                     events: queue.Queue, now: datetime) -> int:
        """Start as many ready tasks as there are free lanes: the first in queue order whose lane and
        repository are free and whose provider is available with a free slot. Returns how many."""
        cands = []
        for t in ready:
            needs = self.needs(t)
            cands.append(Candidate(t.id, tuple(needs) if needs is not None else None,
                                   t.lane_key or str(t.repo), t.repo_key or str(t.repo), self.last_provider(t),
                                   tuple(self.task_models(t).items())))
        picks = self.sched.select_lanes(
            cands, slots,
            {w.task.lane_key or str(w.task.repo) for w in running.values()},
            {w.task.repo_key or str(w.task.repo) for w in running.values()},
            dict(Counter(w.provider for w in running.values() if w.provider)),
            now, self.cfg.lanes - len(running))
        by_id = {t.id: t for t in ready}
        for cand, prov in picks:
            task = by_id[cand.id]
            ts = self.state.task(task.id)
            with self._lock:
                ts.gated_by = None
                if prov:
                    self.sched.current = prov
                self._busy_repos.add(task.repo_key)
                self.state.running[task.id] = {"lane": task.lane_name, "provider": prov,
                                               "attempt": ts.attempts, "out": ""}
            th = threading.Thread(target=self._work, args=(task, prov, events), name=f"baton-{task.lane_name}",
                                  daemon=True)
            running[task.id] = _Worker(task, prov, th)
            th.start()
        return len(picks)

    def _wait_for_providers(self, ready: list[Task], pending: list[Task], slots: dict[str, int],
                            now: datetime) -> bool:
        """Nothing runs and no ready task can start: every provider it may use is limited or gone.
        Block the tasks that may not wait, otherwise sleep until the earliest reset.
        Returns False when the run should end."""
        waits, changed, ended = self._classify_waits(ready, pending, slots, now)
        if ended:
            return False
        if changed:
            return True
        if not waits:
            self.stop_reason = self.stop_reason or "no ready task can start"
            return False
        return self._sleep_until_reset(waits, ready, now)

    def _classify_waits(self, ready: list[Task], pending: list[Task], slots: dict[str, int],
                        now: datetime) -> tuple[list, bool, bool]:
        """For each ready task: block it when it can never run, or note when its providers come back.
        Returns (the waits, whether a task was blocked, whether the run must end)."""
        cfg = self.cfg
        waits = []
        changed = False
        for t in ready:
            allowed = self.needs(t)
            pick = self.sched.pick_for(allowed, self.last_provider(t), now, {}, slots, self.task_models(t))
            label = "verifier" if t.is_verifier else "allowed"
            if pick.provider is None:
                if allowed:
                    # never fall back to a provider outside the task's list
                    self._block(t, f"no {label} provider can run: {pick.reason}")
                    pending.remove(t)
                    changed = True
                    continue
                self.save()
                self.stop_reason = self.stop_reason or pick.reason
                return waits, changed, True
            if pick.wait_until is None:
                continue                # could start; select() only held it back for a lane or a slot
            if allowed and pick.wait_until - now > timedelta(hours=cfg.max_sleep_hours):
                self._block(t, f"{label} providers ({', '.join(allowed)}) are limited until "
                               f"{self.fmt(pick.wait_until)}, more than {cfg.max_sleep_hours:g}h away")
                pending.remove(t)
                changed = True
                continue
            waits.append(pick)
        return waits, changed, False

    def _sleep_until_reset(self, waits: list, ready: list[Task], now: datetime) -> bool:
        """Sleep until the earliest reset among `waits`. False when the run should end instead."""
        cfg = self.cfg
        first = min(waits, key=lambda p: p.wait_until)
        if first.wait_until - now > timedelta(hours=cfg.max_sleep_hours):
            self.save()
            self.stop_reason = self.stop_reason or (
                f"every provider is limited and the earliest reset "
                f"({first.provider}, {self.fmt(first.wait_until)}) is more than {cfg.max_sleep_hours:g}h away")
            return False
        self.refresh(f"sleeping until {self.fmt(first.wait_until)} ({first.reason})")
        self.notify_sleep(first.wait_until, now, first.reason)
        # name the providers the waiting tasks may use: "all providers limited" is only true when they could use any
        used = sorted({p for t in ready for p in (self.needs(t) or self.sched.providers)})
        wording = self.limited_wording(None if set(used) >= set(self.sched.providers) else used)
        self.log(f"{wording}, sleeping until {self.fmt(first.wait_until)} ({first.reason})")
        if not self.clock.sleep_until(first.wait_until, self.stop_wanted):
            self.stop_reason = self.stop_reason or "stop requested"
            return False
        self.log(f"resuming after reset on {first.provider}")
        self.refresh(f"resuming on {first.provider}")
        return True

    def _block(self, task: Task, note: str) -> None:
        """End a task that never started because no provider it may use can run."""
        ts = self.state.task(task.id)
        ts.status, ts.note, ts.finished = "blocked", note, self.clock.now().isoformat()
        self.save()
        self.write_status_file(task)
        self.log(f"task {task.id}: blocked ({ts.commits} commits, {' -> '.join(ts.providers) or '-'}): {note}")
        self.notify("task_blocked", task.id, "blocked", note)

    # ------------------------------------------------------------ worker

    def _work(self, task: Task, prov: str | None, events: queue.Queue) -> None:
        self._tl.task, self._tl.lane = task.id, task.lane_name
        try:
            self.run_task(task, provider=prov)
        except EmergencyBrake as e:
            self.halt_lanes(str(e))
        except RunStopped as e:
            self.stop_reason = self.stop_reason or str(e)
        except DiskFull as e:           # nothing can be saved any more: stop every lane
            self.disk_full = self.disk_full or e
            self.halt_lanes(str(e))
        except Exception as e:          # a bug in this task's run: it fails, the other lanes go on (as in serial mode)
            self.fail_internal(task, e)
        except BaseException as e:      # not an ordinary error (an interpreter exit): end the run
            self._crash = self._crash or e
            self.log(f"task {task.id}: internal error\n{traceback.format_exc()}")
            self.halt_lanes(f"internal error in task {task.id}: {e}")
        finally:
            self.activity = None
            events.put(task.id)

