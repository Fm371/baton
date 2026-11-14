"""Running one agent attempt as a child process."""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .limits import tail_bytes_of
from .isolation import deny_rules, hidden_paths, visible_dirs, with_results_dir, write_claude_settings
from .providers import build_command
from .tasks import Task


def proc_start_time(pid: int) -> str | None:
    """The start time (clock ticks since boot) of a process, from /proc: with the pid it names one
    process for good, so a recycled pid is not mistaken for an agent baton started. None if unknown."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def group_alive(pgid: int) -> bool:
    """Is any live (not zombie) process in this process group? A zombie nobody reaps (a container
    whose init does not) would otherwise look alive for ever."""
    proc = Path("/proc")
    if proc.is_dir():
        try:
            entries = [e for e in proc.iterdir() if e.name.isdigit()]
        except OSError:
            entries = []
        for e in entries:
            try:
                stat = (e / "stat").read_text()
                f = stat[stat.rindex(")") + 2:].split()
            except (OSError, ValueError):
                continue                # gone while we looked
            if len(f) > 2 and f[2] == str(pgid) and f[0] != "Z":
                return True
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def end_group(pgid: int, term_wait: float, kill_wait: float = 5.0) -> bool:
    """End what is left of a process group: SIGTERM, then SIGKILL after `term_wait`. Returns whether
    anything was alive."""
    if not group_alive(pgid):
        return False
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if not group_alive(pgid):
                return True
            time.sleep(0.05)
    return True


class RunStopped(Exception):
    pass


class EmergencyBrake(RunStopped):
    """Too many quick unexplained exits in a row: with lanes, every lane stops, not just the one that saw it."""


@dataclass
class Attempt:
    exit_code: int | None
    started: datetime
    ended: datetime
    timed_out: bool
    out_path: Path
    err_path: Path
    wall_start: float = 0.0     # real time.time(), for comparing file mtimes
    returned_to: str | None = None   # stopped so the task could go back to this provider
    stalled: bool = False       # ended by baton because the output stopped growing ([run] stall_minutes)
    stall_note: str = ""        # the last line the agent printed before it stalled
    resumed: bool = False       # operator requested a fresh session through the owning runner

    @property
    def seconds(self) -> float:
        return (self.ended - self.started).total_seconds()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _last_line(path: Path, limit: int = 200) -> str:
    """The last non-empty line of a file, shortened; for the note on a stalled attempt."""
    for raw in reversed(tail_bytes_of(path, 8000).decode("utf-8", errors="replace").splitlines()):
        if raw.strip():
            text = " ".join(raw.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


@dataclass
class _Wait:
    """How waiting for one agent ended."""
    code: int | None = None
    timed_out: bool = False
    returned_to: str | None = None
    stalled: bool = False
    stall_note: str = ""
    resumed: bool = False


class _StallWatch:
    """Notices an agent whose output stopped growing (`[run] stall_minutes`; 0 = off)."""

    def __init__(self, stall_s: float, out_path: Path, err_path: Path) -> None:
        self.seconds = stall_s
        self.paths = (out_path, err_path)
        self.seen_size, self.grown_at = sum(_size(p) for p in self.paths), time.monotonic()
        self.checked_at = self.grown_at

    def check(self) -> tuple[bool, str]:
        """(stalled, the last line it printed). Call it after each wait slice."""
        if not self.seconds:
            return False, ""
        size = sum(_size(p) for p in self.paths)
        now = time.monotonic()
        gap = now - self.checked_at
        self.checked_at = now
        # A long observation gap (suspend, clock jump, descheduling) is not proof of a hung provider.
        if gap > max(5.0, min(30.0, self.seconds / 4) + 5):
            self.grown_at = now
        if size != self.seen_size:
            self.seen_size, self.grown_at = size, now
            return False, ""
        if now - self.grown_at >= self.seconds:
            return True, last_activity(*self.paths)
        return False, ""


def last_activity(out_path: Path, err_path: Path) -> str:
    from .watch import render
    lines = tail_bytes_of(out_path, 8000).decode("utf-8", errors="replace").splitlines()
    meaningful = []
    import json
    for line in lines:
        try:
            ev = json.loads(line)
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "command_execution":
                meaningful.append(f"command completed (exit {item.get('exit_code')}): {item.get('command', '')}")
            else:
                meaningful.extend(render(line))
        except (ValueError, AttributeError):
            meaningful.extend(render(line))
    return "; ".join([*meaningful[-2:], _last_line(err_path)])[:1000].strip("; ")


class ProcessMixin:
    """Methods of baton.runner.Runner, split out of it; they use its attributes."""

    kill_grace_s = 30.0      # a stop: SIGTERM first, SIGKILL for an agent still alive after this
    group_grace_s = 3.0      # after an agent exits: what it left running gets SIGTERM, then SIGKILL after this

    # one child per worker thread (one in all, unless [run] lanes > 1)
    @property
    def _child(self):
        return self._children.get(threading.get_ident())

    @_child.setter
    def _child(self, p) -> None:
        if p is None:
            self._children.pop(threading.get_ident(), None)
        else:
            self._children[threading.get_ident()] = p

    def kill_children(self, force: bool = False) -> None:
        """End every live child's process group: SIGTERM, and SIGKILL once `kill_grace_s` has passed
        (a signal, or the emergency brake). `force`: SIGKILL now (a second Ctrl+C)."""
        for child in list(self._children.values()):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if not force and self._children:
            timer = threading.Timer(self.kill_grace_s, self._escalate)
            timer.daemon = True
            timer.start()

    def _escalate(self) -> None:
        """The grace period is over: an agent that ignored SIGTERM gets SIGKILL."""
        if any(c.poll() is None for c in list(self._children.values())):
            self.log("an agent ignored SIGTERM; sending SIGKILL")
            self.kill_children(force=True)

    def register_agent(self, task: Task, child: subprocess.Popen) -> None:
        """Write the agent's pid to run.json, so a run that dies can be cleaned up after."""
        with self._lock:
            self.state.agents[task.id] = {"pid": child.pid, "pgid": child.pid,
                                          "start": proc_start_time(child.pid), "repo": str(task.repo)}
            self.save()

    def forget_agent(self, task: Task, pgid: int) -> None:
        """The agent has exited: end whatever it left running (a dev server, a `nohup` job), then drop its pid."""
        if end_group(pgid, self.group_grace_s):
            self.log(f"task {task.id}: the agent left processes running; they were ended")
        if group_alive(pgid):
            self._resume_refused = True
            raise RunStopped(f"task {task.id}: process group {pgid} survived bounded cleanup; resume refused")
        with self._lock:
            if self.state.agents.pop(task.id, None) is not None:
                self.save()

    def end_leftover_agents(self) -> None:
        """Agents of an earlier run that died without cleaning up (kill -9, power loss, a closed terminal)
        may still be working in a repository. End them before this run touches it."""
        for tid, a in list(self.state.agents.items()):
            pid, pgid = int(a.get("pid", 0)), int(a.get("pgid", 0))
            same = proc_start_time(pid)
            if pid != pgid:
                raise RunStopped(f"task {tid}: recorded pid/group do not identify an owned session; no replacement started")
            if pgid <= 1 or (same is not None and a.get("start") and same != a["start"]):
                continue                # the pid now belongs to another process
            if group_alive(pgid) and (not same or not a.get("start")):
                raise RunStopped(f"task {tid}: cannot establish ownership of leftover group {pgid}; "
                                 "no replacement started; inspect the recorded agent identity")
            if end_group(pgid, self.group_grace_s):
                self.log(f"task {tid}: ended an agent (pid {pid}) left running by a run that died")
            if group_alive(pgid):
                raise RunStopped(f"task {tid}: leftover group {pgid} survived cleanup; no replacement started")
        with self._lock:
            self.state.agents = {}
            self.save()

    # --- one attempt -----------------------------------------------------------------------------
    # How each provider is set up before its command is built. A new provider needs one new
    # entry here (and one in providers.build_command); `execute` itself does not change.
    _LAUNCH_SETUP = {"claude": "_claude_launch", "codex": "_codex_launch"}

    def _claude_launch(self, task: Task, pc, stem: Path, d: Path) -> dict:
        hidden = hidden_paths(self.cfg, task, self.verifiers)
        # always written: it carries the deny rules and turns Claude Code's own commit trailer off
        user = str(self.cfg.base_dir / task.settings) if task.settings else pc.settings
        settings = str(write_claude_settings(user, deny_rules(hidden), Path(f"{stem}.settings.json"),
                                             self.cfg.base_dir))
        # a verifier keeps its add_dirs: it must write its result file, and
        # its deny rules stop it reading tasks/ and other tasks' results
        claude_dirs = with_results_dir(pc.add_dirs, self.cfg.results_dir, task.repo)
        add_dirs = claude_dirs if task.is_verifier else visible_dirs(claude_dirs, hidden)
        return {"settings": settings, "add_dirs": add_dirs,
                "max_turns": task.max_turns or self.cfg.max_turns or None}

    def _codex_launch(self, task: Task, pc, stem: Path, d: Path) -> dict:
        # The repository is already writable under workspace-write. Only this
        # task's results folder (not all of results/) and the configured extra
        # directories sit outside it and need explicit writable mounts. Older
        # configs put the shared add_dirs only under Claude, so carry those
        # across as well as the Codex-specific ones. A hidden path is never
        # mounted (verifiers do not run on Codex, see preflight).
        shared_dirs = self.cfg.providers.get("claude")
        add_dirs = list(dict.fromkeys([
            str(d),
            *(shared_dirs.add_dirs if shared_dirs else []),
            *pc.add_dirs,
        ]))
        return {"settings": None,
                "add_dirs": visible_dirs(add_dirs, hidden_paths(self.cfg, task, self.verifiers)),
                "max_turns": None}

    def _launch_setup(self, task: Task, prov: str, stem: Path, d: Path) -> dict:
        pc = self.cfg.providers[prov]
        name = self._LAUNCH_SETUP.get(prov)
        setup = getattr(self, name)(task, pc, stem, d) if name else {}
        return {"settings": None, "add_dirs": None, "max_turns": None, **setup}

    def _wait_for_agent(self, task: Task, watch: str | None, out_path: Path, err_path: Path) -> "_Wait":
        """Wait for the running child: it exits, runs out of time, is handed back to the preferred
        provider, or stalls (its output stops growing). The child is ended in the last three cases."""
        child = self._child
        deadline = time.monotonic() + 60 * (task.timeout_min or self.cfg.task_timeout_min)
        stall_min = task.stall_minutes if task.stall_minutes is not None else self.cfg.stall_minutes
        stall = _StallWatch(60 * stall_min, out_path, err_path)
        res = _Wait()
        while True:
            try:
                step = max(self._wait_step(deadline, watch, stall.seconds), 0)
                before = time.monotonic()
                res.code = child.wait(timeout=step)
                return res
            except subprocess.TimeoutExpired:
                gap = time.monotonic() - before
                if gap > step + 5:
                    deadline += gap - step   # do not spend timeout/retries on an observation gap
                    stall.grown_at = before + gap  # the same gap grants a fresh stall interval, even near its deadline
                self.apply_operator_ops(resume_only=True)
                if self.state.task(task.id).resume_pending == self.state.task(task.id).attempts:
                    res.resumed, res.stall_note = True, last_activity(out_path, err_path)
                    res.code = self._kill(child)
                    return res
                if self._end_if_due(res, child, deadline, watch, stall, task):
                    return res

    def _wait_step(self, deadline: float, watch: str | None, stall_s: float) -> float:
        left = deadline - time.monotonic()
        step = min(left, self.cfg.return_check_s) if watch else left
        if stall_s:
            step = min(step, max(1.0, min(30.0, stall_s / 4)))
        return min(step, 1.0)  # control protocol stays responsive even with watchdog off

    def _end_if_due(self, res: "_Wait", child: subprocess.Popen, deadline: float, watch: str | None,
                    stall: "_StallWatch", task: Task) -> bool:
        """After a wait slice: end the child if it is out of time, the preferred provider is back, or
        it stalled. Returns whether it was ended."""
        if time.monotonic() >= deadline:
            res.timed_out = True
        elif watch and self.sched.available(watch, self.clock.now()):
            res.returned_to = watch         # the preferred provider has reset: hand the task back
        else:
            res.stalled, res.stall_note = stall.check()
            if not res.stalled:
                return False
        if res.stalled:
            with self._lock:
                ts = self.state.task(task.id)
                ts.stall_retries = max(ts.stall_retries, 1)
                ts.last_end = f"watchdog interrupted attempt {ts.attempts}; last activity: {res.stall_note}"
                self.save()  # durable before termination/WIP: a crash cannot replenish the retry budget
        res.code = self._kill(child)
        return True

    def _clock_step_back(self, task: Task, wall: float, mono0: float) -> float:
        """A result file counts as this attempt's when its mtime is not before `wall_start`. If the wall
        clock was stepped back during the attempt (NTP, a manual change), a file written after the step
        has an older mtime than the start and would be taken for a stale one. The step shows as the
        difference between wall-clock and monotonic elapsed time; move wall_start back by it. The price:
        a stale file written within that many seconds before the start passes as fresh. Only a backwards
        step beyond one second changes anything (suspend makes the difference positive, so it is ignored)."""
        jump = (time.time() - wall) - (time.monotonic() - mono0)
        if jump < -1.0:
            wall += jump
            self.log(f"task {task.id}: the system clock moved back {-jump:.0f}s during the attempt; "
                     "result files are judged against the adjusted start")
        return wall

    def execute(self, task: Task, prov: str, spec, prompt: str, n: int,
                watch: str | None = None) -> Attempt:
        d = self.cfg.results_dir / task.id
        d.mkdir(parents=True, exist_ok=True)
        stem = d / f"attempt-{n:02d}.{prov}"
        prompt_path = Path(f"{stem}.prompt.md")
        prompt_path.write_text(prompt, encoding="utf-8")
        out_path, err_path = Path(f"{stem}.out"), Path(f"{stem}.err")
        setup = self._launch_setup(task, prov, stem, d)
        pc = self.cfg.providers[prov]
        if prov == "codex" and task.sandbox:
            pc = dataclasses.replace(pc, sandbox=task.sandbox)       # this task's own sandbox mode
        argv, extra_env = build_command(pc, spec, setup["max_turns"],
                                        settings=setup["settings"], add_dirs=setup["add_dirs"])
        env = {**os.environ, **extra_env}
        started = self.clock.now()
        wall = time.time()
        mono0 = time.monotonic()
        with prompt_path.open("rb") as fin, out_path.open("wb") as fout, err_path.open("wb") as ferr:
            try:
                self._child = subprocess.Popen(argv, cwd=task.repo, stdin=fin, stdout=fout, stderr=ferr,
                                               env=env, start_new_session=True)
            except OSError as e:
                ferr.write(f"baton: could not start {argv[0]}: {e}\n".encode())
                return Attempt(127, started, self.clock.now(), False, out_path, err_path, wall)
            pgid = self._child.pid
            self.register_agent(task, self._child)
            if self.stop_requested:         # a stop that came between the checks and the start
                self.kill_children()
            try:
                res = self._wait_for_agent(task, watch, out_path, err_path)
            finally:
                self._child = None
                self.forget_agent(task, pgid)
        wall = self._clock_step_back(task, wall, mono0)
        return Attempt(res.code, started, self.clock.now(), res.timed_out, out_path, err_path, wall,
                       returned_to=res.returned_to, stalled=res.stalled, stall_note=res.stall_note, resumed=res.resumed)

    def _kill(self, p: subprocess.Popen) -> int | None:
        for sig, grace in ((signal.SIGTERM, self.kill_grace_s), (signal.SIGKILL, 5)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                return p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        return p.poll()

    def save_fixture(self, task: Task, prov: str, n: int, att: Attempt) -> None:
        """Keep unexplained exits: they are the next limit-message format to learn."""
        d = self.cfg.state_dir / "fixtures"
        d.mkdir(parents=True, exist_ok=True)
        base = d / f"{self.run_id}-{task.id}-{n:02d}.{prov}"
        for src, ext in ((att.out_path, "out"), (att.err_path, "err")):
            try:
                Path(f"{base}.{ext}").write_bytes(tail_bytes_of(src, 200_000))
            except OSError:
                pass
