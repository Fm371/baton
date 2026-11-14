"""`baton watch`: a live, readable view of the current attempt's agent output.

An attempt's raw output is the CLI's JSON event stream (results/<task>/
attempt-NN.<provider>.out). This follows the newest one, prints what the agent
says and runs, one line per event, and moves on to the next attempt when the run
starts one. It ends when the run does.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, TextIO

from .config import Config
from .state import load_state

ATTEMPT_RE = re.compile(r"^attempt-(\d+)\.(claude|codex)\.out$")
MAX_LINE = 300


def clip(text: str, n: int = MAX_LINE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def run_active(cfg: Config) -> bool:
    """True while a baton run holds the lock of this folder. Read-only: it never creates the
    folder or the lock file, and it asks with a shared lock, so a probe cannot make a run that
    is starting at the same moment see "already active"."""
    path = cfg.state_dir / "lock"
    try:
        f = path.open("rb")
    except OSError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def lock_info(cfg: Config) -> dict | None:
    """What the running baton wrote into its lock file: {"pid", "started", "run"}; None if unreadable."""
    try:
        data = json.loads((cfg.state_dir / "lock").read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def active_message(cfg: Config) -> str:
    """The text for a second `baton run` in a folder that already has one: who holds it and how to see it."""
    info = lock_info(cfg) or {}
    who = ""
    if info.get("pid") and pid_alive(info["pid"]):
        who = f" (pid {info['pid']}" + (f", since {info['started']}" if info.get("started") else "") + ")"
    return (f"another baton run is already active in this folder{who}; `baton status` shows what it is "
            "doing, `baton stop` ends it after the current task")


def attempts(cfg: Config) -> list[Path]:
    return [p for d in sorted(cfg.results_dir.glob("*/")) if d.is_dir()
            for p in d.iterdir() if ATTEMPT_RE.match(p.name)]


def _newest(paths):
    return max(paths, key=lambda p: (p.stat().st_mtime, p.name), default=None)


def _running_tasks(cfg: Config) -> tuple[set, dict]:
    """(ids of the tasks marked running, the live lanes) from run.json; empty when it cannot be read."""
    try:
        st = load_state(cfg.state_dir / "run.json")
        return {tid for tid, t in st.tasks.items() if t.status == "running"}, st.running
    except (OSError, ValueError):
        return set(), {}


def latest_attempt(cfg: Config, lane: str | None = None) -> Path | None:
    """The output of the running task's newest attempt, else the newest attempt of all.
    With lanes: that of the named lane, or of the first busy lane."""
    running, live = _running_tasks(cfg)
    files = attempts(cfg)
    if lane is not None:
        ids = {tid for tid, v in live.items() if v.get("lane") == lane}
        return _newest(p for p in files if p.parent.name in ids)
    if live:
        first = next(iter(live))
        mine = [p for p in files if p.parent.name == first]
        if mine:
            return _newest(mine)
    return _newest([p for p in files if p.parent.name in running] or files)


def tool_summary(name: str, args) -> str:
    if isinstance(args, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description"):
            if args.get(key):
                return f"{name}: {clip(args[key], 200)}"
    return name


def _content(ev: dict) -> list:
    return (ev.get("message") or {}).get("content") or []


def _r_system(ev: dict) -> list[str]:
    return [f"· session {ev.get('session_id', '')[:8]} started"] if ev.get("subtype") == "init" else []


def _r_assistant(ev: dict) -> list[str]:
    out: list[str] = []
    for block in _content(ev):
        if block.get("type") == "text" and block.get("text", "").strip():
            out.append("agent: " + clip(block["text"]))
        elif block.get("type") == "tool_use":
            out.append("▸ " + tool_summary(block.get("name", "tool"), block.get("input")))
    return out


def _r_user(ev: dict) -> list[str]:
    return ["  ✗ " + clip(block.get("content", "")) for block in _content(ev)
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")]


def _r_result(ev: dict) -> list[str]:
    return [("✗ " if ev.get("is_error") else "✓ ") + "result: " + clip(ev.get("result", ""))]


def _r_thread(ev: dict) -> list[str]:
    return [f"· thread {str(ev.get('thread_id', ''))[:8]} started"]


def _item_started(item: dict) -> list[str]:
    if item.get("type") == "command_execution":
        return ["▸ " + clip(item.get("command", ""), 200)]
    return []


def _item_completed(item: dict) -> list[str]:
    itype = item.get("type")
    if itype == "agent_message":
        return ["agent: " + clip(item.get("text", ""))]
    if itype == "reasoning":
        return ["· " + clip(item.get("text", ""), 160)]
    if itype == "command_execution" and item.get("exit_code") not in (0, None):
        return [f"  ✗ exit {item.get('exit_code')}: " + clip(item.get("aggregated_output", ""), 160)]
    if itype == "file_change":
        return ["▸ edited " + ", ".join(clip(c.get("path", ""), 80) for c in item.get("changes", [])[:5])]
    return []


def _r_item_started(ev: dict) -> list[str]:
    return _item_started(ev.get("item") or {})


def _r_item_completed(ev: dict) -> list[str]:
    return _item_completed(ev.get("item") or {})


def _r_error(ev: dict) -> list[str]:
    return ["✗ " + clip(ev.get("message") or json.dumps(ev.get("error", ev)))]


def _r_turn_completed(ev: dict) -> list[str]:
    usage = ev.get("usage") or {}
    return [f"· turn done ({usage.get('output_tokens', '?')} output tokens)"]


# event type -> how to show it (Claude stream-json, then Codex --json); an unknown type shows nothing
RENDERERS = {
    "system": _r_system, "assistant": _r_assistant, "user": _r_user, "result": _r_result,
    "thread.started": _r_thread, "item.started": _r_item_started, "item.completed": _r_item_completed,
    "error": _r_error, "turn.failed": _r_error, "turn.completed": _r_turn_completed,
}


def render(line: str) -> list[str]:
    """Readable lines for one line of a CLI's event stream; non-JSON text is kept as it is."""
    line = line.rstrip("\n")
    if not line.strip():
        return []
    try:
        ev = json.loads(line)
    except ValueError:
        return [clip(line)]
    if not isinstance(ev, dict):
        return [clip(line)]
    kind = ev.get("type")
    handler = RENDERERS.get(kind) if isinstance(kind, str) else None
    return handler(ev) if handler else []


BACKLOG_BYTES = 2_000_000      # an output file can be gigabytes: start the view near its end


def open_at_backlog(path: Path):
    """The file opened for reading, positioned so that at most BACKLOG_BYTES of it are left (on a line start)."""
    f = path.open("r", encoding="utf-8", errors="replace")
    try:
        size = path.stat().st_size
    except OSError:
        return f
    if size > BACKLOG_BYTES:
        f.seek(size - BACKLOG_BYTES)
        f.readline()                    # drop the partial line
    return f


def describe(path: Path) -> str:
    m = ATTEMPT_RE.match(path.name)
    return f"task {path.parent.name}, attempt {int(m.group(1))} on {m.group(2)}" if m else path.name


def _first_attempt(cfg: Config, out: TextIO, follow: bool, active: Callable[[], bool], poll: float,
                   sleep: Callable[[float], None], lane: str | None) -> Path | None:
    """The attempt to show; with `follow`, wait for the first one while the run is active."""
    path = latest_attempt(cfg, lane)
    while path is None:
        if not follow or not active():
            print("no attempt output yet: nothing has run in this folder", file=out)
            return None
        sleep(poll)
        path = latest_attempt(cfg, lane)
    return path


def _header(path: Path, out: TextIO) -> None:
    print(f"== {describe(path)}  ({path}) ==", file=out, flush=True)


def _drain(f, partial: str, show, out: TextIO) -> tuple[str, bool]:
    """Print what was appended to the file; returns (the unfinished last line, whether anything came)."""
    chunk = f.read()
    if not chunk:
        return partial, False
    *whole, partial = (partial + chunk).split("\n")
    for ln in whole:
        for r in show(ln):
            print(r, file=out)
    out.flush()
    return partial, True


def _next_attempt(cfg: Config, lane: str | None, path: Path) -> Path | None:
    """A newer attempt than `path` (the run moved on), else None."""
    newer = latest_attempt(cfg, lane)
    if newer is not None and newer != path and newer.stat().st_mtime >= path.stat().st_mtime:
        return newer
    return None


def watch(cfg: Config, out: TextIO, follow: bool = True, lines: int = 30, raw: bool = False,
          poll: float = 0.5, sleep: Callable[[float], None] = time.sleep,
          active: Callable[[], bool] | None = None, lane: str | None = None) -> int:
    """Print the tail of the current attempt, then follow it while the run is active."""
    active = active or (lambda: run_active(cfg))
    show = (lambda s: [s.rstrip("\n")] if s.strip() else []) if raw else render
    path = _first_attempt(cfg, out, follow, active, poll, sleep, lane)
    if path is None:
        return 1
    _header(path, out)
    f = open_at_backlog(path)
    try:
        backlog = [r for ln in f.read().splitlines() for r in show(ln)]
        for r in backlog[-lines:]:
            print(r, file=out)
        out.flush()
        if not follow:
            return 0
        partial, quiet = "", 0
        while True:
            partial, got = _drain(f, partial, show, out)
            if got:
                quiet = 0
                continue
            newer = _next_attempt(cfg, lane, path)
            if newer is not None:
                f.close()
                path, partial, quiet = newer, "", 0
                _header(path, out)
                f = open_at_backlog(path)
                continue
            if not active():
                quiet += 1
                if quiet >= 2:              # one more pass to catch a last write
                    print("== the run is not active ==", file=out, flush=True)
                    return 0
            sleep(poll)
    finally:
        f.close()
