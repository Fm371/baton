"""Services a run needs (a test database, a container) and how to check them.

`[services.mongo] check = "mongosh --port 27017 --eval 'rs.status().ok'"` declares a health check. baton runs it
in preflight, before every task that lists the service in `needs = ["mongo"]` (every task with
`[run] services_for_all_tasks = true`), and before a repo-suite. A failing check gets one `restart`, then the
run stops with a clear message instead of letting an agent burn an attempt against a dead database (tests
that need it would hang, or pass on mocks).

The commands come from baton.toml, which its owner writes; nothing from a task file or an agent is ever
put into them. A string runs through /bin/sh (so a pipe or a quoted argument works), a list as an argv.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config, ServiceConfig
from .tasks import Task


@dataclass
class Down:
    name: str
    detail: str
    restarted: bool = False


def _run(cmd: str | list[str], timeout_s: float) -> tuple[bool, str]:
    """(exit code was 0, a short detail)."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, errors="replace",
                           timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout_s:g}s"
    except OSError as e:
        return False, f"could not run: {e}"
    if p.returncode == 0:
        return True, "ok"
    tail = " ".join((p.stderr or p.stdout or "").split())[-160:]
    return False, f"exit {p.returncode}" + (f": {tail}" if tail else "")


def check(svc: ServiceConfig) -> tuple[bool, str]:
    return _run(svc.check, svc.timeout_s)


def needed(cfg: Config, task: Task) -> list[str]:
    """The declared services this task needs: its `needs`, or all of them with services_for_all_tasks."""
    if not cfg.services or not (task.runs_agent or task.template == "repo-suite"):
        return []
    if cfg.services_for_all_tasks:
        return list(cfg.services)
    return [n for n in task.needs if n in cfg.services]


def ensure(cfg: Config, names: list[str], log: Callable[[str], None] = lambda m: None,
           sleep: Callable[[float], None] = time.sleep) -> list[Down]:
    """Check each service; for one that fails, run its `restart` once and check again.
    Returns the services that are still down (empty: all healthy)."""
    down = []
    for name in names:
        svc = cfg.services[name]
        ok, detail = check(svc)
        if ok:
            continue
        restarted = False
        if svc.restart:
            log(f"service {name} is down ({detail}); restarting it")
            r_ok, r_detail = _run(svc.restart, max(svc.timeout_s, 60.0))
            restarted = True
            if not r_ok:
                log(f"service {name}: restart failed ({r_detail})")
            sleep(svc.wait_s)
            ok, detail = check(svc)
            if ok:
                log(f"service {name} is healthy again after a restart")
                continue
        down.append(Down(name, detail, restarted))
    return down


def describe(down: list[Down]) -> str:
    return "; ".join(f"service '{d.name}' is down ({d.detail})" + (" even after a restart" if d.restarted else "")
                     for d in down)


def prompt_line(cfg: Config, names: list[str]) -> str | None:
    """The sentence that tells the agent which services baton just verified, so it does not hunt for others."""
    if not names:
        return None
    parts = [f"{n} at {cfg.services[n].endpoint}" if cfg.services[n].endpoint else n for n in names]
    return ("Services available (baton checked them healthy just before this task started): " + ", ".join(parts)
            + ". Use exactly these; do not start other instances or look for them on other ports. If one is "
              "unreachable from your sandbox, say so in the result file instead of mocking it.")


# ---------------------------------------------------------------------------- reachability probes

def host_can_connect(addr: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    host, _, port = addr.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout_s):
            return True, "connected"
    except OSError as e:
        return False, str(e)


SANDBOX_SCRIPT = (
    "import socket, sys\n"
    "h, p = sys.argv[1], int(sys.argv[2])\n"
    "try:\n"
    "    socket.create_connection((h, p), timeout=3).close()\n"
    "except OSError as e:\n"
    "    print('BLOCKED:' + str(e)); sys.exit(3)\n"
    "print('REACHED')\n"
)


def sandbox_can_connect(argv_prefix: list[str], addr: str, timeout_s: float = 30.0) -> tuple[bool | None, str]:
    """Try the connection from inside a sandbox. `argv_prefix` is the sandbox command up to the program
    to run (for Codex: [codex, sandbox, "--"]). True: reached. False: the sandbox blocked it. None: the
    probe itself did not work (unknown syntax in this CLI version), nothing can be said."""
    host, _, port = addr.rpartition(":")
    try:
        p = subprocess.run([*argv_prefix, sys.executable, "-c", SANDBOX_SCRIPT, host.strip("[]"), port],
                           capture_output=True, text=True, errors="replace", timeout=timeout_s,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"the sandbox probe did not run: {e}"
    out = (p.stdout or "")
    if "REACHED" in out:
        return True, "reached from inside the sandbox"
    if "BLOCKED:" in out:
        return False, out.split("BLOCKED:", 1)[1].strip()[:160]
    tail = " ".join((p.stderr or out).split())[-160:]
    return None, f"the sandbox probe gave no answer (exit {p.returncode}): {tail}"


# ---------------------------------------------------------------------------- preflight

def _declared_service_findings(cfg: Config, tasks: list[Task]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    used = sorted({n for t in tasks for n in needed(cfg, t)})
    for name in used:
        svc = cfg.services[name]
        ok, detail = check(svc)
        if ok:
            continue
        who = ", ".join(t.id for t in tasks if name in needed(cfg, t))[:120]
        if svc.restart:
            warnings.append(f"service '{name}' is down ({detail}); baton will run its `restart` once before the "
                            f"first task that needs it ({who})")
        else:
            errors.append(f"service '{name}' is down ({detail}) and has no `restart`; start it before the run "
                          f"(needed by {who})")
    for name in sorted(set(cfg.services) - set(used)):
        warnings.append(f"service '{name}' is declared but no task needs it (add needs = [\"{name}\"] to "
                        "the tasks, or set [run] services_for_all_tasks = true)")
    return errors, warnings


def _sandbox_reach_finding(codex, addr: str) -> tuple[str | None, str | None]:
    """(error, warning) about reaching `addr` from inside Codex's sandbox; (None, None) when it can or
    there is no usable sandbox to try it from."""
    from . import sandbox
    from .providers import CODEX_NETWORK_ARGS, resolve_bin

    path = resolve_bin(codex)
    if not path or sandbox.probe(codex)[0] is not True:
        return None, None       # the sandbox warnings already say so
    prefix = [path, "sandbox", *(CODEX_NETWORK_ARGS if codex.network else ()), "--"]
    reached, why = sandbox_can_connect(prefix, addr)
    if reached is False:
        return (f"codex's sandbox cannot reach {addr} ({why})"
                + (" even with providers.codex.network = true" if codex.network
                   else "; tests that need it would hang or fail inside Codex. Set "
                        "[providers.codex] network = true, or keep those tasks on Claude (providers = [\"claude\"])"),
                None)
    if reached is None:
        return None, f"could not check {addr} from inside codex's sandbox: {why}"
    return None, None


def _tcp_findings(cfg: Config, enabled: list[str]) -> tuple[list[str], list[str]]:
    from . import sandbox

    errors: list[str] = []
    warnings: list[str] = []
    codex = cfg.providers.get("codex")
    if codex and codex.network and codex.sandbox not in ("workspace-write", None, ""):
        warnings.append(f"providers.codex.network = true has no effect with sandbox = \"{codex.sandbox}\"")
    from_sandbox = bool(codex and "codex" in enabled and codex.sandbox not in sandbox.NO_SANDBOX)
    for addr in cfg.preflight.services_tcp:
        ok, detail = host_can_connect(addr)
        if not ok:
            errors.append(f"preflight.services_tcp: {addr} is not reachable from this machine ({detail}); "
                          "start the service before the run")
            continue
        if not from_sandbox:
            continue
        err, warn = _sandbox_reach_finding(codex, addr)
        errors += [err] if err else []
        warnings += [warn] if warn else []
    return errors, warnings


def preflight_findings(cfg: Config, tasks: list[Task], enabled: list[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for `baton plan`: declared services that are down, TCP endpoints the agents'
    sandbox cannot reach, a `network` setting that has no effect."""
    errors, warnings = _declared_service_findings(cfg, tasks)
    tcp_errors, tcp_warnings = _tcp_findings(cfg, enabled)
    return errors + tcp_errors, warnings + tcp_warnings
