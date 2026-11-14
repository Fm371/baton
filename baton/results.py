"""Reading what an agent left behind: the STATUS and COMMIT lines of a result
file, and the verdict in Codex's last message."""

from __future__ import annotations

import json
import re
from pathlib import Path

STATUS_RE = re.compile(r"^STATUS:\s*(done|partial|blocked)\s*$")
COMMIT_RE = re.compile(r"^COMMIT:\s*(\S.*)\s*$")
HEADER_MAX = 100       # commitlint header-max-length


def read_status(path: Path, since: float) -> str | None:
    """The STATUS line of a result file written during this attempt."""
    try:
        if path.stat().st_mtime < since - 1:
            return None
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return None
    m = STATUS_RE.match(first.strip())
    return m.group(1) if m else None


def read_commit_message(path: Path, since: float) -> str | None:
    """The COMMIT line of a result file written during this attempt."""
    try:
        if path.stat().st_mtime < since - 1:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        m = COMMIT_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def read_codex_status(output: str) -> str | None:
    """Read STATUS from Codex's last agent_message JSON event."""
    last_message = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item", {})
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            last_message = item["text"]
    if last_message is None:
        return None
    for line in last_message.splitlines():
        m = STATUS_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


REASON_MAX = 160
_SKIP_LINE = re.compile(r"^(#|STATUS:|COMMIT:|-{3,}|={3,}|\*{3,}|\|)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
# headings where an agent says why a task did not finish cleanly, most telling first
_REASON_HEADINGS = ("reason", "why", "blockers", "blocker", "blocked", "issues", "problems", "not done",
                    "remaining", "open questions", "summary")


def _first_prose(lines: list[str]) -> str:
    in_fence = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s or _SKIP_LINE.match(s):
            continue
        s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s)
        s = " ".join(s.split())
        return s if len(s) <= REASON_MAX else s[:REASON_MAX - 1].rstrip() + "…"
    return ""


def reason_line(path: Path) -> str:
    """The one-line reason in a result file: the first sentence of the section where agents say why
    (Issues, Blockers, Reason, ...), else the first prose after the STATUS line (a heading, a code
    fence or a table row does not count). Empty when the file is missing."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    sections: dict[str, list[str]] = {}
    current = None
    for ln in lines:
        m = _HEADING.match(ln.strip())
        if m:
            current = m.group(1).strip().lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(ln)
    for name in _REASON_HEADINGS:
        for title, body in sections.items():
            if title == name or title.startswith(name + " ") or title.endswith(" " + name):
                found = _first_prose(body)
                if found:
                    return found
    return _first_prose(lines)
