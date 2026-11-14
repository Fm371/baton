"""Which task is next: dependencies, gates, approval checkpoints, built-in
checks, and picking up new task files with --follow."""

from __future__ import annotations

from pathlib import Path

from . import approval, checks, ops
from .autocheck import schedule_preflight
from .config import ConfigError, load_config
from .process import RunStopped
from .state import FINISHED, atomic_write
from .tasks import Task, TaskError, ancestors, load_tasks, order_tasks, parse_task_file


class QueueMixin:
    """Methods of baton.runner.Runner, split out of it; they use its attributes."""

    def reload(self, ordered: list[Task], by_id: dict[str, Task], seen: set[str]
               ) -> tuple[list[Task], dict[str, Task], list[Task]]:
        """Read the task folders again (--follow): the tasks that were not there
        before, which are queued unless they finished in an earlier run."""
        try:
            fresh = order_tasks(schedule_preflight(self.cfg, load_tasks(self.cfg)))
        except (TaskError, ConfigError) as e:
            msg = str(e)
            if msg != self._reload_error:
                self._reload_error = msg
                self.log(f"task folders not reloaded: {msg.splitlines()[0]}")
            return ordered, by_id, []
        self._reload_error = None
        changed = self._relink(fresh, by_id)
        new = [t for t in fresh if t.id not in seen]
        if not new and not changed:
            return ordered, by_id, []
        return fresh, {t.id: t for t in fresh}, self._adopt(fresh, new, seen) + changed

    def _adopt(self, fresh: list[Task], new: list[Task], seen: set[str]) -> list[Task]:
        """Take the freshly read task list as the run's; the new tasks that still have to run."""
        seen.update(t.id for t in new)
        self.ordered, self.verifiers = fresh, [t for t in fresh if t.is_verifier]
        if new:
            self.log("new task(s): " + ", ".join(t.id for t in new))
        return [t for t in new if self.state.task(t.id).status not in FINISHED
                and not (t.is_approval and self.state.task(t.id).status == "rejected")]

    LINK_FIELDS = ("depends_on", "after")

    def _relink(self, fresh: list[Task], by_id: dict[str, Task]) -> list[Task]:
        """A task file whose `depends_on` or `after` was edited while the run is going: the tasks that
        have not started take the new links, so the next pick honours them. A checkpoint that was waiting
        for a decision and now has an unfinished dependency goes back to pending (and is returned, to be
        queued again); its request file is rewritten when it is asked again."""
        requeue: list[Task] = []
        for f in fresh:
            old = by_id.get(f.id)
            if old is None or self.state.task(f.id).status in FINISHED or self.state.task(f.id).status == "running":
                continue
            edits = [(k, getattr(old, k), getattr(f, k)) for k in self.LINK_FIELDS if getattr(old, k) != getattr(f, k)]
            if not edits:
                continue
            for k, was, now in edits:
                setattr(old, k, list(now))
                self.log(f"task {f.id}: {k} changed in the task file: {was} -> {now}")
            ts = self.state.task(f.id)
            if f.is_approval and ts.status == "awaiting":
                by_new = {t.id: t for t in fresh}
                if any(d in by_new and not self.satisfied(by_new[d]) for d in f.depends_on):
                    ts.status, ts.note, ts.finished = "pending", "dependencies changed; asked again when they are done", None
                    self.log(f"task {f.id}: back to pending, a dependency was added that is not finished")
                    self.save()
                    requeue.append(old)
        return requeue

    def apply_decisions(self) -> set[str]:
        """Take `baton approve` / `baton reject` into the run state; returns the
        checkpoints decided now, which must not be asked again in this run."""
        self.apply_operator_ops()
        decided: set[str] = set()
        for t in self.ordered:
            ts = self.state.task(t.id)
            if not t.is_approval or ts.status not in ("awaiting", "rejected"):
                continue
            d = approval.read_decision(self.cfg, t.id)
            if not d:
                continue
            approval.retire_decision(self.cfg, t.id)
            if d["decision"] == "approve":
                ts.status, ts.note = "done", "approved" + (f": {d['note']}" if d.get("note") else "")
            else:
                ts.status, ts.note = "rejected", d.get("note") or "rejected"
            ts.finished = self.clock.now().isoformat()
            self.log(f"task {t.id}: {ts.status} by decision ({ts.note})")
            if ts.status == "rejected":
                self.block_dependents(t)
            decided.add(t.id)
            self.write_status_file(t)
            self.save()
        return decided

    def apply_operator_ops(self) -> None:
        """`baton mark` and `baton providers reset` typed while this run is active: applied here,
        between tasks, by the one process that writes the state."""
        for op in ops.take_ops(self.cfg):
            try:
                with self._lock:
                    if op.get("op") == "mark":
                        task = ops.find_task(self.ordered, str(op.get("task")))
                        result = Path(op["result"]) if op.get("result") else None
                        ops.apply_mark(self, task, str(op.get("status")), str(op.get("note") or ""), result)
                    elif op.get("op") == "retry":
                        ops.apply_retry(self, ops.find_task(self.ordered, str(op.get("task"))))
                    elif op.get("op") == "providers_reset" and op.get("provider") in self.sched.status:
                        ops.apply_providers_reset(self, op["provider"])
                    else:
                        self.log(f"operator command not understood: {op}")
            except ops.OpsError as e:
                self.log(f"operator command ignored: {e}")

    # fields of a task file that may change between attempts; where it runs and what it depends on may not
    RELOAD_TASK_FIELDS = ("work_type", "rung", "resolved_rung", "timeout_min", "max_turns", "providers",
                          "body", "priority")
    # settings of baton.toml read again before each attempt; providers' enabled/bin and the paths need a restart
    RELOAD_RUN_FIELDS = ("routing", "task_timeout_min", "max_turns", "reset_margin_s", "unknown_limit_cooldown_min",
                         "max_sleep_hours", "quick_exit_s", "max_quick_unknown", "transient_wait_s",
                         "max_transient", "no_result_retries", "verifier_test_paths", "hardcoded_key_check",
                         "return_mid_task", "return_check_s")
    RELOAD_PROVIDER_FIELDS = ("extra_args", "env", "settings", "permission_mode", "effort_flag", "add_dirs",
                              "sandbox", "profile")

    def _reload_task(self, task: Task) -> list[str]:
        """Read the task's front matter again; the changes, in words."""
        if not (task.runs_agent and task.path.is_file()):
            return []
        kind = "verify" if task.path.parent == self.cfg.verifiers_dir else "implement"
        try:
            fresh = parse_task_file(task.path, self.cfg, kind)
        except (TaskError, ConfigError, OSError) as e:
            self._warn_reload(f"task {task.id}: {task.path.name} not reloaded: {str(e).splitlines()[0]}")
            return []
        changes: list[str] = []
        for f in self.RELOAD_TASK_FIELDS:
            old, new = getattr(task, f), getattr(fresh, f)
            if old != new:
                changes.append("the task text" if f == "body" else f"{f} {old!r} -> {new!r}")
            setattr(task, f, new)
        return changes

    def _apply_setting(self, key: tuple, old, new) -> str | None:
        """Apply one changed setting of baton.toml; a line for the log, or None when it needs a restart."""
        if key[0] == "restart":
            self._warn_reload(f"baton.toml providers.{key[1]}.{key[2]} changed; "
                              "it takes effect when the run is started again")
            return None
        if key[0] == "run":
            setattr(self.cfg, key[1], new)
        else:
            setattr(self.cfg.providers[key[1]], key[2], new)
        return (f"baton.toml {'.'.join(key)}" if key[-1] == "routing"
                else f"baton.toml {'.'.join(key)} {old!r} -> {new!r}")

    def _reload_config(self) -> list[str]:
        """Read baton.toml again; the changes, in words."""
        if self.cfg.source is None or not self.cfg.source.is_file():
            return []
        try:
            new_cfg = load_config(self.cfg.source)
        except ConfigError as e:
            self._warn_reload(f"baton.toml not reloaded: {str(e).splitlines()[0]}")
            return []
        seen, now_vals = self._cfg_seen, self.config_values(new_cfg)
        changes = [line for key, new in now_vals.items() if new != seen.get(key)
                   if (line := self._apply_setting(key, seen.get(key), new))]
        self._cfg_seen = now_vals
        return changes

    def reload_for_attempt(self, task: Task) -> None:
        """Before each new attempt: read the task's front matter and baton.toml again, so a change made
        while the run is going (a provider list, a rung, a timeout, an extra argument) takes effect
        without stopping the run. What changed is logged. A file that no longer parses is ignored,
        once, with the reason."""
        with self._lock:
            changes = self._reload_task(task) + self._reload_config()
            if changes:
                self.log(f"task {task.id}: changed since the last attempt: " + "; ".join(changes))

    def config_snapshot(self) -> dict[tuple, object]:
        """The reloadable settings as the file has them now. The file, not self.cfg: a setting changed
        in memory (a test, a caller) is not an edit of baton.toml and must not be undone by a reload."""
        try:
            if self.cfg.source is not None and self.cfg.source.is_file():
                return self.config_values(load_config(self.cfg.source))
        except ConfigError:
            pass
        return self.config_values(self.cfg)

    def config_values(self, cfg) -> dict[tuple, object]:
        """The settings that may change while a run is going, keyed by where they live in baton.toml."""
        out: dict[tuple, object] = {("run", f): getattr(cfg, f) for f in self.RELOAD_RUN_FIELDS}
        for name, pc in cfg.providers.items():
            for f in self.RELOAD_PROVIDER_FIELDS:
                out[("providers", name, f)] = getattr(pc, f)
            for f in ("bin", "enabled"):
                out[("restart", name, f)] = getattr(pc, f)
        return out

    def _warn_reload(self, msg: str) -> None:
        """Log a reload problem once, not before every attempt."""
        warned = getattr(self, "_reload_warned", set())
        if msg not in warned:
            warned.add(msg)
            self._reload_warned = warned
            self.log(msg)

    def release_dependents(self, task: Task) -> list[str]:
        """A task just ended in a state its dependents can follow (a verifier `done`, any other `done` or
        `partial`): the tasks it held back (gated by it, or skipped because it had not finished) are
        looked at again. Without this a retried verifier leaves "gated by V1" notes behind, and a
        checkpoint skipped earlier in the run is never offered again."""
        if not self.satisfied(task):
            return []
        by_id = {t.id: t for t in self.ordered}
        freed: list[str] = []
        with self._lock:
            for t in self.ordered:
                ds = self.state.task(t.id)
                if t.id == task.id or ds.rejected_by:
                    continue
                if ds.status == "gated" and ds.gated_by == task.id:
                    pass
                elif ds.status == "skipped" and task.id in t.depends_on \
                        and all(self.satisfied(by_id[d]) for d in t.depends_on if d in by_id):
                    pass
                else:
                    continue
                ds.status, ds.note, ds.gated_by = "pending", "", None
                freed.append(t.id)
            if freed:
                self._freed_ids = getattr(self, "_freed_ids", set()) | set(freed)
                self.save()
        if freed:
            self.log(f"task {task.id} ended {self.state.task(task.id).status}: "
                     f"{', '.join(freed)} can be picked again")
        return freed

    def take_freed(self, pending: list[Task]) -> None:
        """Put the tasks released since the last look back into the queue of this run (in place)."""
        ids = getattr(self, "_freed_ids", set())
        if not ids or self.only:
            return
        self._freed_ids = set()
        have = {t.id for t in pending}
        by_id = {t.id: t for t in self.ordered}
        pending += [by_id[i] for i in ids if i in by_id and i not in have
                    and self.state.task(i).status == "pending"]

    def block_dependents(self, checkpoint: Task) -> None:
        by_id = {t.id: t for t in self.ordered}
        note = self.state.task(checkpoint.id).note
        for t in self.ordered:
            ts = self.state.task(t.id)
            if t.id == checkpoint.id or ts.status in FINISHED or ts.status == "running":
                continue
            if checkpoint.id in {a.id for a in ancestors(t, by_id)}:
                ts.status, ts.rejected_by = "blocked", checkpoint.id
                ts.note = f"approval {checkpoint.id} rejected: {note}"
                self.log(f"task {t.id}: blocked ({ts.note})")

    @staticmethod
    def needs(t: Task) -> list[str] | None:
        """The providers a task may use; [] for a checkpoint or built-in check, which needs none."""
        return t.providers if t.runs_agent else []

    def _block_rejected(self, task: Task, ts, rejected: str) -> None:
        ts.status, ts.rejected_by = "blocked", rejected
        ts.note = f"approval {rejected} rejected: {self.state.task(rejected).note}"
        self.save()
        self.log(f"task {task.id}: blocked ({ts.note})")

    def _note_waiting(self, ts, waiting: list[str]) -> None:
        """Held, not skipped: the task runs once the checkpoint is approved."""
        note = f"waiting for approval of {', '.join(waiting)}"
        if ts.note != note:
            ts.note = note
            self.save()

    def _gate_or_skip(self, task: Task, ts, missing: list[str], by_id: dict[str, Task]) -> None:
        gate = self.gate_for(missing, by_id)
        with self._lock:        # a lane's worker may be saving the state
            if gate:
                ts.status, ts.gated_by = "gated", gate
                ts.note = f"gated by verifier {gate} ({self.state.task(gate).status})"
            else:
                ts.status, ts.gated_by = "skipped", None
                ts.note = f"dependencies not finished: {', '.join(missing)}"
            self.save()
        self.log(f"task {task.id}: {ts.status} ({ts.note})")

    def _settle(self, task: Task, pending: list[Task], ids: set[str], by_id: dict[str, Task]) -> bool:
        """Mark a task whose dependency ended without being met as blocked, gated or skipped and drop it
        from `pending`. True when it was dropped; False when it is ready or still waiting."""
        missing = [d for d in self.deps_of(task, by_id) if not self.satisfied(by_id[d])]
        if not missing or self.only or any(d in ids for d in missing):
            return False
        ts = self.state.task(task.id)
        rejected = self.rejection_for(missing)
        if rejected:
            self._block_rejected(task, ts, rejected)
        else:
            waiting = [d for d in missing if self.state.task(d).status == "awaiting"]
            if waiting:
                self._note_waiting(ts, waiting)
                return False
            self._gate_or_skip(task, ts, missing, by_id)
        pending.remove(task)
        return True

    def ready_tasks(self, pending: list[Task], by_id: dict[str, Task]) -> list[Task]:
        """The pending tasks whose dependencies are met, in queue order. A task
        whose dependency ended without being met is marked skipped or gated and
        dropped from pending; one whose dependency is still pending waits."""
        changed = True
        while changed:
            changed = False
            ids = {t.id for t in pending}
            for task in list(pending):
                if self._settle(task, pending, ids, by_id):
                    changed = True
        return [t for t in pending
                if self.only or (all(self.satisfied(by_id[d]) for d in self.deps_of(t, by_id))
                                 and self.after_ended(t, by_id)
                                 and not self.held_for_last(t, pending))]

    # an `after` task has ended once it will not run again in this run, whatever the result
    AFTER_ENDED = ("done", "partial", "failed", "blocked", "skipped", "rejected")

    def after_ended(self, task: Task, by_id: dict[str, Task]) -> bool:
        """`after` is ordering only: no gate, no containment, no skip when the other task failed."""
        return all(d not in by_id or self.state.task(d).status in self.AFTER_ENDED for d in task.after)

    def deps_of(self, task: Task, by_id: dict[str, Task]) -> list[str]:
        """What a task waits for. An explicit `depends_on` on a verifier stays a real gate, also
        with `verifiers_last`: only verifiers nobody depends on are moved to the end of the run."""
        return list(task.depends_on)

    def trailing_verifiers(self) -> set[str]:
        """With `verifiers_last`, the verifiers that no task depends on: they gate nothing, so they
        run after every other task. A verifier somebody depends on keeps its place as a gate."""
        if not self.cfg.verifiers_last:
            return set()
        depended = {d for t in self.ordered for d in t.depends_on}
        return {t.id for t in self.ordered if t.is_verifier and t.id not in depended}

    def held_for_last(self, task: Task, pending: list[Task]) -> bool:
        """With `verifiers_last`, a verifier nobody depends on waits until no other task is left."""
        trailing = self.trailing_verifiers()
        return (task.id in trailing
                and any(t.id not in trailing for t in pending))

    def satisfied(self, dep: Task) -> bool:
        """A task may follow a finished task; past a verifier only when it is done."""
        status = self.state.task(dep.id).status
        return status == "done" if dep.is_verifier or dep.is_approval else status in FINISHED

    def rejection_for(self, missing: list[str]) -> str | None:
        """The rejected checkpoint a task is blocked by, directly or through a blocked dependency."""
        for d in missing:
            st = self.state.task(d)
            if st.status == "rejected":
                return d
            if st.status == "blocked" and st.rejected_by:
                return st.rejected_by
        return None

    def gate_for(self, missing: list[str], by_id: dict[str, Task]) -> str | None:
        """The verifier that holds a task back, directly or through a gated dependency."""
        for d in missing:
            if by_id[d].is_verifier:
                return d
            if self.state.task(d).status == "gated" and self.state.task(d).gated_by:
                return self.state.task(d).gated_by
        return None

    def run_approval(self, task: Task) -> None:
        """A human checkpoint: write what needs a decision and hold the dependents."""
        ts = self.state.task(task.id)
        ts.started = ts.started or self.clock.now().isoformat()
        ts.status, ts.note, ts.finished = "awaiting", "waiting for a human: baton approve / baton reject", None
        by_id = {t.id: t for t in self.ordered}
        statuses = {tid: s.status for tid, s in self.state.tasks.items()}
        atomic_write(self.cfg.results_dir / f"{task.id}.md",
                     approval.request_text(self.cfg, task, by_id, statuses))
        self.write_status_file(task)
        self.save()
        self.log(f"task {task.id}: awaiting approval; see results/{task.id}.md")

    def run_check(self, task: Task) -> None:
        """A built-in verifier (env-preflight, browser-smoke, real-data-smoke, repo-suite): baton runs it
        itself and a gap makes it blocked, which gates every task that depends on it."""
        ts = self.state.task(task.id)
        ts.status, ts.started, ts.attempts = "running", self.clock.now().isoformat(), ts.attempts + 1
        self.refresh(f"running {task.template}")
        self.log(f"task {task.id}: start ({task.template})")
        ctx = checks.CheckContext(self.cfg, task, self.cfg.results_dir / task.id, lambda: self.stop_requested)
        result = checks.run_template(ctx)
        atomic_write(self.cfg.results_dir / f"{task.id}.md", result.markdown(task, self.cfg.results_dir))
        if self.stop_requested:
            ts.status = "pending"
            raise RunStopped(self.stop_reason or "stop requested")
        self.finish(task, result.status,
                    f"{len(result.gaps)} gap(s), see results/{task.id}.md" if result.gaps else "")
