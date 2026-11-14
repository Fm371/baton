"""`[run] on_event`: tell someone when something happens in an unattended run.

    on_event = "ntfy publish my-baton"           # or ["curl", "-d", "$BATON_NOTE", ...] as a list
    on_event_sleep_min = 30                      # a sleep for a reset at least this long is an event

The command is split into arguments (shlex) and run directly, never through a shell, so nothing in a task
note can be interpreted as shell syntax. What happened arrives in environment variables:

    BATON_EVENT   task_done | task_partial | task_failed | task_blocked | sleep | run_end | run_stopped | run_died
    BATON_TASK    the task id ("" for run events)
    BATON_STATUS  the task's final status ("" otherwise)
    BATON_NOTE    one line: the task's note, why the run sleeps or stopped
    BATON_RUN_ID  the run's id

A hook that fails, hangs (killed after `on_event_timeout_seconds`) or is missing is logged and ignored: it can
never change the outcome of the run. Events are sent from a background thread so a slow endpoint does not hold
up the queue; the end of the run waits briefly for the last ones. A run killed with SIGKILL or power loss
cannot report itself: the next run reports `run_died` when it finds work left mid-way.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Callable

NOTE_MAX = 500
TASK_EVENTS = {"done": "task_done", "partial": "task_partial", "failed": "task_failed", "blocked": "task_blocked"}


def one_line(text: str | None, limit: int = NOTE_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Notifier:
    def __init__(self, argv: list[str] | None, timeout_s: float, run_id: str, log: Callable[[str], None]):
        self.argv = argv
        self.timeout_s = timeout_s
        self.run_id = run_id
        self.log = log
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.argv)

    def emit(self, event: str, task: str = "", status: str = "", note: str = "") -> None:
        if not self.argv:
            return
        env = {**os.environ, "BATON_EVENT": event, "BATON_TASK": task, "BATON_STATUS": status,
               "BATON_NOTE": one_line(note), "BATON_RUN_ID": self.run_id}
        t = threading.Thread(target=self._send, args=(event, task, env), daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
        t.start()

    def _send(self, event: str, task: str, env: dict[str, str]) -> None:
        try:
            p = subprocess.run(self.argv, env=env, capture_output=True, text=True, errors="replace",
                               timeout=self.timeout_s, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                tail = " ".join((p.stderr or p.stdout or "").split())[-120:]
                self.log(f"on_event {event}{' ' + task if task else ''}: the hook exited {p.returncode}"
                         + (f" ({tail})" if tail else ""))
        except subprocess.TimeoutExpired:
            self.log(f"on_event {event}{' ' + task if task else ''}: the hook did not finish in "
                     f"{self.timeout_s:g}s and was stopped")
        except Exception as e:      # missing binary, permission: logged, never raised into the run
            self.log(f"on_event {event}{' ' + task if task else ''}: could not run the hook: {e}")

    def wait(self, seconds: float = 20.0) -> None:
        """Let the events in flight finish (the end of a run), but never hold the exit up for long."""
        with self._lock:
            pending = list(self._threads)
        for t in pending:
            t.join(seconds)
