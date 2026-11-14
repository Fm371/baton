"""Is the Codex sandbox usable here?

Codex runs commands inside a bubblewrap sandbox. It fails when the kernel
refuses unprivileged user namespaces, typically after a reboot reset
`kernel.apparmor_restrict_unprivileged_userns` on Ubuntu, or when baton was
started from inside another agent's sandbox. Every Codex command then fails with
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`, and the agent
reports its task as blocked with nothing done. That is the provider's fault, not
the task's: baton probes the sandbox up front, and when an attempt shows the
error, takes Codex out of the run and retries the task on the next provider.
"""

from __future__ import annotations

import os
import re
import subprocess

from .config import ProviderConfig
from .providers import resolve_bin

SANDBOX_ERROR_RE = re.compile(
    r"bwrap:[^\n]{0,160}?(?:Failed RTM_NEWADDR|Operation not permitted|Permission denied|namespace|uid map)",
    re.I)
DEFAULT_PROBE = ["sandbox", "--", "true"]
NO_SANDBOX = (None, "", "danger-full-access")

HINT = ("On Ubuntu 24.04 and later the usual cause is AppArmor restricting unprivileged user "
        "namespaces: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (lost on reboot; "
        "put it in /etc/sysctl.d/ to keep it). Also do not launch baton from inside another agent's "
        "sandbox (a Claude Code or Codex session): sandboxes do not nest. Start it from a plain terminal.")


def sandbox_error(*texts: str) -> str | None:
    """The first bwrap error line in the given output, or None."""
    for text in texts:
        m = SANDBOX_ERROR_RE.search(text or "")
        if m:
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            return text[line_start:line_end if line_end != -1 else None].strip()[:200]
    return None


def probe(pc: ProviderConfig) -> tuple[bool | None, str]:
    """(True, ...) when the sandbox works or none is configured, (False, error line)
    when it is broken, (None, why) when the probe could not decide: the probe command
    is wrong for this Codex version, or the sandbox is not used."""
    if pc.sandbox in NO_SANDBOX:
        return True, "no sandbox configured"
    args = DEFAULT_PROBE if pc.sandbox_probe is None else pc.sandbox_probe
    if not args:
        return None, "sandbox probe turned off (providers.codex.sandbox_probe = [])"
    path = resolve_bin(pc)
    if not path:
        return None, f"{pc.bin} not found"
    try:
        p = subprocess.run([path, *args], capture_output=True, text=True, timeout=60,
                           env={**os.environ, **pc.env}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"sandbox probe did not run: {e}"
    if p.returncode == 0:
        return True, "sandbox probe passed"
    line = sandbox_error(p.stderr, p.stdout)
    if line:
        return False, line
    tail = " ".join((p.stderr or p.stdout).split())[-200:]
    return None, f"sandbox probe `{' '.join(args)}` exited {p.returncode} without a sandbox error: {tail}"
