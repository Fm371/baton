"""Dev servers for the checks that need a running app: reuse one that already
answers, start the others, stop only what baton started."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator


class CheckError(Exception):
    """The check could not run (as opposed to running and finding gaps)."""


def reachable(url: str, timeout: float = 2.0) -> bool:
    """True when something answers HTTP at `url`; any status counts."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _tail(path: Path, n: int = 8) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _start_server(spec: dict, name: str, url: str, base_dir: Path, out_dir: Path, env: dict[str, str] | None):
    """Start one server in its own process group; (process, log path, the command as written)."""
    if not spec.get("cmd"):
        raise CheckError(f"{name}: nothing answers at {url} and no cmd is configured to start it")
    cwd = Path(os.path.expanduser(spec.get("cwd", ".")))
    cwd = cwd if cwd.is_absolute() else base_dir / cwd
    log = out_dir / f"server-{name}.log".replace("/", "_")
    cmd = spec["cmd"]
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    with log.open("wb") as fout:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdout=fout, stderr=subprocess.STDOUT,
                                    env={**os.environ, **(env or {}), **spec.get("env", {})},
                                    start_new_session=True)
        except OSError as e:
            raise CheckError(f"{name}: could not start `{cmd}` in {cwd}: {e}") from e
    return proc, log, cmd


def _wait_until_answering(proc: subprocess.Popen, name: str, url: str, cmd, log: Path, timeout_s: float,
                          should_stop: Callable[[], bool]) -> None:
    deadline = time.monotonic() + timeout_s
    while not reachable(url):
        if proc.poll() is not None:
            raise CheckError(f"{name}: `{cmd}` exited with {proc.returncode} before answering at {url}\n"
                             + _tail(log))
        if should_stop() or time.monotonic() > deadline:
            raise CheckError(f"{name}: no answer at {url} after {timeout_s:g}s\n" + _tail(log))
        time.sleep(0.3)


def _stop_server(proc: subprocess.Popen) -> None:
    """SIGTERM the server's process group, SIGKILL if it is still there after ten seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=10)
            break
        except subprocess.TimeoutExpired:
            continue


@contextlib.contextmanager
def dev_servers(specs: list[dict], base_dir: Path, out_dir: Path, timeout_s: float,
                should_stop: Callable[[], bool] = lambda: False, reuse: bool = True,
                env: dict[str, str] | None = None) -> Iterator[list[str]]:
    """Yields log lines saying what was reused and started. Each spec has `url`,
    and `cmd` (a command line) with optional `name`, `cwd` and `env`."""
    started: list[subprocess.Popen] = []
    notes: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for spec in specs:
            name, url = spec.get("name") or spec["url"], spec["url"]
            if reuse and reachable(url):
                notes.append(f"{name}: reusing the server already answering at {url}")
                continue
            proc, log, cmd = _start_server(spec, name, url, base_dir, out_dir, env)
            started.append(proc)
            _wait_until_answering(proc, name, url, cmd, log, timeout_s, should_stop)
            notes.append(f"{name}: started `{cmd}`, answering at {url}")
        yield notes
    finally:
        for proc in started:
            _stop_server(proc)
