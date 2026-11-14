"""SUMMARY.md, status.json and metrics.jsonl."""

from __future__ import annotations

import json
from datetime import datetime

from . import branchreport, notverified, timefmt, versioninfo
from .results import reason_line
from .state import DiskFull, atomic_write, is_disk_full
from .tasks import Task

# a task that ended without success: the one-line reason from its own result file is added to the note
NEEDS_REASON = ("partial", "blocked", "failed")


class ReportMixin:
    """Methods of baton.runner.Runner, split out of it; they use its attributes."""

    def fmt(self, dt: datetime | None) -> str:
        """ISO time with the UTC offset and how far away it is: 2026-10-31 18:32 +03:30 (in 2h10m)."""
        return timefmt.when(dt, self.tz, self.clock.now())

    def metrics(self, row: dict) -> None:
        with self._lock:
            try:
                self.cfg.results_dir.mkdir(parents=True, exist_ok=True)
                with (self.cfg.results_dir / "metrics.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as e:
                if is_disk_full(e):
                    raise DiskFull(str(self.cfg.results_dir)) from e
                raise

    def write_status_file(self, task: Task) -> None:
        ts = self.state.task(task.id)
        atomic_write(self.cfg.results_dir / task.id / "status.json", json.dumps({
            "task": task.id, "status": ts.status, "reason": ts.reason, "commits": ts.commits,
            "attempts": ts.attempts,
            "providers": ts.providers, "note": ts.note, "finished": ts.finished,
            "not_verified": notverified.reported(self.cfg.results_dir, task),
        }, indent=2, ensure_ascii=False) + "\n")

    @staticmethod
    def status_label(ts) -> str:
        """The status as shown to people: a blocked task says whether the environment or a finding blocked
        it, and a partial one that the environment, not the code, left it partial."""
        if ts.status == "blocked" and ts.reason in ("env", "finding"):
            return f"blocked:{ts.reason}"
        if ts.status == "partial" and ts.reason == "env":
            return "partial (env-limited)"
        return ts.status

    @staticmethod
    def kind_label(t: Task) -> str:
        if t.is_approval:
            return " (approve)"
        if t.template:
            return f" (verify: {t.template})"
        return " (verify)" if t.is_verifier else ""

    def write_summary(self, ordered: list[Task]) -> None:
        with self._lock:
            atomic_write(self.cfg.results_dir / "SUMMARY.md", self.summary_text(ordered))

    def write_branch_report(self, ordered: list[Task]) -> None:
        """results/BRANCHES.md at the end of a run: the branches it left, conflicts between them, a merge order."""
        try:
            text = branchreport.report(self.cfg, ordered, self.state)
        except Exception as e:      # a report must never fail the run
            self.log(f"branch report not written: {type(e).__name__}: {e}")
            return
        if text:
            atomic_write(self.cfg.results_dir / "BRANCHES.md", text)
            n = text.count("conflict** in")
            self.log("branches left by the run: results/BRANCHES.md"
                     + (f" ({n} conflicting pair(s))" if n else " (no conflicts between them)"))

    def shown_status(self, ts, ended: bool) -> str:
        """A task that was mid-attempt when the run ended is `interrupted` (the next run continues it);
        otherwise the status with its environment/finding label."""
        return "interrupted" if ts.status == "running" and ended else self.status_label(ts)

    def _task_row(self, t: Task, ended: bool) -> str:
        ts = self.state.task(t.id)
        act = self.activity_of(t.id)
        note = act if ts.status == "running" and act and not ended else ts.note
        if ts.status in NEEDS_REASON:
            reason = reason_line(self.cfg.results_dir / f"{t.id}.md")
            if reason and reason not in note:
                note = f"{note}; {reason}" if note else reason
        if ts.overridden_by:
            note = f"{note} [set by {ts.overridden_by}]" if note else f"[set by {ts.overridden_by}]"
        return (f"| {t.id}{self.kind_label(t)} | {t.repo.name} | {self.shown_status(ts, ended)} | "
                f"{ts.commits} | {ts.attempts} | "
                f"{' -> '.join(ts.providers) or '-'} | {note.replace('|', '/') or '-'} |")

    def _provider_line(self, p: str, now) -> str:
        s = self.sched.status[p]
        state = ("limited until " + self.fmt(s.limited_until)
                 if s.limited_until and s.limited_until > now else "available")
        live = {m: u for m, u in s.model_limits.items() if u > now}
        if live and state == "available":
            state = "available except " + ", ".join(
                f"{m} (until {self.fmt(u)})" for m, u in sorted(live.items()))
        if s.disabled:
            state = f"disabled ({s.disabled})"
        return f"- {p}: {state}" + (" (current)" if p == self.sched.current else "")

    def _lanes_markdown(self) -> str:
        if not self.lanes_on:
            return ""
        live_tasks = [f"- {v['lane']}: {tid}" + (f" on {v['provider']}" if v.get("provider") else "")
                      for tid, v in self.state.running.items()]
        return "## Lanes\n\n" + ("\n".join(live_tasks) if live_tasks else "- none busy") + "\n\n"

    def summary_text(self, ordered: list[Task], ended: bool | None = None) -> str:
        """SUMMARY.md as text. ended=True: no run is working on the tasks any more."""
        ended = self._ended if ended is None else ended
        rows = ["| task | repo | status | commits | attempts | providers | note |",
                "|---|---|---|---|---|---|---|"] + [self._task_row(t, ended) for t in ordered]
        now = self.clock.now()
        prov = [self._provider_line(p, now) for p in self.sched.providers]
        stopped = f" — stopped: {self.stop_reason}" if self.stop_reason else ""
        version = self.state.baton_version or versioninfo.describe()
        return (f"# baton run summary · baton {version}\n\nUpdated {timefmt.iso(now, self.tz)}{stopped}\n\n"
                + notverified.warnings_markdown(self.cfg, ordered, self.state)
                + notverified.needs_markdown(self.cfg, ordered, self.state)
                + "\n".join(rows) + "\n\n" + self._lanes_markdown() + self.branches_markdown(ordered)
                + "## Providers\n\n" + "\n".join(prov) + "\n\n"
                + notverified.markdown(notverified.collect(self.cfg, ordered, self.state)))

    def branches_markdown(self, ordered: list[Task]) -> str:
        """Where each task's work is: its branch and commit count. A verifier's tests live on
        `verify/<id>` and are not in the implementation branch until someone merges them."""
        rows = []
        for t in ordered:
            ts = self.state.task(t.id)
            if not (t.runs_agent and t.branch) or not (ts.attempts or ts.commits):
                continue
            kind = " (verify branch)" if t.is_verifier else ""
            rows.append(f"| {t.id} | {t.repo.name} | `{t.branch}`{kind} | {ts.commits} | "
                        f"{t.base or 'the checkout it started from'} |")
        if not rows:
            return ""
        return ("## Branches\n\n| task | repo | branch | commits | started from |\n|---|---|---|---|---|\n"
                + "\n".join(rows) + "\n\nBranches are never merged or pushed by baton"
                + ("; see `results/BRANCHES.md` for conflicts between them and a merge order"
                   if (self.cfg.results_dir / "BRANCHES.md").is_file() else "") + ".\n\n")
