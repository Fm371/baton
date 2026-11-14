"""Can code inside the Codex sandbox create a socketpair and a localhost TCP connection?

`codex --sandbox workspace-write` can start fine (baton's sandbox probe passes) and still refuse
what many test suites need: `socket.socketpair()` (asyncio event loops, Starlette's TestClient
build one) and a connection to localhost (a test database on 127.0.0.1). The symptom is a suite
that stalls or fails with `PermissionError: [Errno 1] Operation not permitted`, a task that ends
`partial`, and a check that passes on your own machine in a fraction of a second.

This probe runs a tiny Python script twice, directly and inside `codex sandbox -- ...`. It reports
only a difference: a restriction that the host does not have is the sandbox's. It never decides
anything on its own; `baton plan` prints a warning with the exact fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from .config import ProviderConfig
from .providers import CODEX_NETWORK_ARGS, resolve_bin
from .sandbox import NO_SANDBOX

SCRIPT = r"""
import json, socket
out = {}
try:
    a, b = socket.socketpair()
    a.close(); b.close()
    out["socketpair"] = "ok"
except OSError as e:
    out["socketpair"] = f"{type(e).__name__}: {e}"
try:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname(), timeout=3)
    cli.close(); srv.close()
    out["tcp"] = "ok"
except OSError as e:
    out["tcp"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
"""
TIMEOUT_S = 30


def _run(argv: list[str], env: dict[str, str]) -> dict[str, str] | None:
    """The probe's JSON result, or None when the command failed or printed nothing usable."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "socketpair" in data:
            return {str(k): str(v) for k, v in data.items()}
    return None


def sandbox_prefix(pc: ProviderConfig, path: str) -> list[str] | None:
    """`codex sandbox --` (the part of the configured probe before the command), or None when the
    probe was turned off or does not look like `sandbox -- <command>`."""
    args = ["sandbox", "--", "true"] if pc.sandbox_probe is None else pc.sandbox_probe
    if "--" not in args:
        return None
    cut = args.index("--")
    return [path, *args[:cut], *_provider_flags(pc), "--"]


def _provider_flags(pc: ProviderConfig) -> list[str]:
    """The sandbox settings real tasks run with, so the probe tests the same sandbox, not codex's default."""
    flags: list[str] = []
    if pc.sandbox:
        flags += ["-c", f'sandbox_mode="{pc.sandbox}"']
    if getattr(pc, "network", False):
        flags += list(CODEX_NETWORK_ARGS)
    return flags


FIX = ("set `[providers.codex] network = true` in baton.toml (it adds "
       "`-c sandbox_workspace_write.network_access=true`). Be aware that this opens the whole internet "
       "to the agent, not only localhost; the alternative is to run such tests outside the sandbox "
       "(a verifier on Claude, or `repo-suite`, which baton runs itself)")


def check(pc: ProviderConfig, runner=None) -> list[str]:
    """Warnings (at most one) for `baton plan`; [] when nothing is restricted or the probe cannot tell."""
    if pc.sandbox in NO_SANDBOX or pc.sandbox_probe == []:
        return []
    path = resolve_bin(pc)
    if not path:
        return []
    runner = runner or _run
    prefix = sandbox_prefix(pc, path)
    if prefix is None:
        return []
    env = {**os.environ, **pc.env}
    host = runner([sys.executable, "-c", SCRIPT], env)
    inside = runner([*prefix, sys.executable, "-c", SCRIPT], env)
    if host is None or inside is None:
        return []                       # cannot compare: say nothing rather than guess
    blocked = [f"{what} ({inside[what]})" for what in ("socketpair", "tcp")
               if host.get(what) == "ok" and inside.get(what, "ok") != "ok"]
    if not blocked:
        return []
    already = bool(getattr(pc, "network", False))
    return [f"codex: the sandbox refuses {' and '.join(blocked)} that this machine allows. Test suites that "
            "build an event loop (Starlette TestClient, asyncio) or connect to a local database will stall or "
            "fail inside it and their tasks end `partial`. "
            + ("`[providers.codex] network = true` is already set, so the restriction comes from somewhere else "
               "(the sandbox mode or the launch environment)." if already else f"To allow it, {FIX}.")]
