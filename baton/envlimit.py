"""Is a failed attempt the environment's fault rather than the code's?

A provider's sandbox can forbid things a test suite needs: Codex's workspace-write sandbox has no
network and no `socketpair`, so Starlette's TestClient, a localhost database or an HTTP call fails with
`PermissionError: [Errno 1] Operation not permitted`. The task then ends `partial` ("the full suite is
not green") although nothing is wrong with the code, and everything that depends on it inherits the
doubt. This module recognises the common signatures so baton can say "env-limited" instead.

It only looks for specific restrictions: a permission error right after a socket call, a blocked
connect, a sandbox refusal. A generic failure is never classified here. Output is searched as raw text,
so a traceback inside a JSON event (newlines written as `\\n`) matches the same way as a plain one.
"""

from __future__ import annotations

import re

_ERR = r"(?:PermissionError|Operation not permitted|EPERM)"
_WINDOW = r"[\s\S]{0,500}?"

# (pattern, short name of the restriction); the first match wins
SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"socketpair{_WINDOW}{_ERR}|{_ERR}{_WINDOW}socketpair", re.I),
     "the sandbox forbids socketpair (TestClient and asyncio need it)"),
    (re.compile(rf"(?:socket\.socket|create_connection|\.connect\(|\.bind\(|getaddrinfo|open_connection)"
                rf"{_WINDOW}{_ERR}", re.I),
     "the sandbox forbids sockets (PermissionError on a socket call)"),
    (re.compile(r"connect\w*[^\n]{0,100}(?:Operation not permitted|EPERM)", re.I),
     "the sandbox blocks outgoing connections (Operation not permitted)"),
    (re.compile(r"Network is unreachable[^\n]{0,80}(?:sandbox|bwrap)|(?:sandbox|bwrap)[^\n]{0,120}Network is unreachable",
                re.I),
     "the sandbox has no network"),
    (re.compile(r"bwrap:[^\n]{0,160}?(?:Operation not permitted|Permission denied|namespace|uid map|RTM_NEWADDR)", re.I),
     "the sandbox (bwrap) could not start"),
]


def detect(*texts: str | None) -> str | None:
    """A short description of the sandbox/environment restriction the texts show, or None."""
    for text in texts:
        if not text:
            continue
        for rx, name in SIGNATURES:
            if rx.search(text):
                return name
    return None
