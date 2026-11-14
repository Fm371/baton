"""Which baton wrote a run: "0.3.0 (abc1234)", so a run that used an old checkout can be told
from one that used the fixed one."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import __version__

_cache: str | None = None


def describe() -> str:
    """The version, plus the short commit when baton runs from a git checkout (best effort)."""
    global _cache
    if _cache is None:
        _cache = __version__
        root = Path(__file__).resolve().parent.parent
        if (root / ".git").exists():
            try:
                p = subprocess.run(["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=7"],
                                   capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
                if p.returncode == 0 and p.stdout.strip():
                    _cache = f"{__version__} ({p.stdout.strip()})"
            except (OSError, subprocess.SubprocessError):
                pass
    return _cache


def parse(version: str) -> tuple[int, ...]:
    """'0.3.1' -> (0, 3, 1); anything that is not a dotted number is ignored from there on."""
    nums = []
    for part in str(version).split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        nums.append(int(m.group(0)))
    return tuple(nums)


def older_than(installed: str, minimum: str) -> bool:
    return parse(installed) < parse(minimum)
