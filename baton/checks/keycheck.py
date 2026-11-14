"""A cheap static check for hardcoded data keys.

A resource type, plan kind or role is data: its key lives in the database or
in config. A literal such as `type_key == "person"` passes on fixtures that
create that key and fails on a real database that names it differently. This
scans the lines a task added for string literals assigned to, compared with or
looked up under a name that ends in `key` or `slug`, and for `resource_type`
style names. Test files and lines marked `baton: ok` are skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import gitutil
from ..isolation import matches_any

SOURCE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".java", ".kt", ".rb", ".php",
              ".rs", ".cs", ".vue", ".svelte"}
NAME_BODY = (r"""(?P<name>\w*_key|\w*Key|\w*_slug|\w*Slug|key|slug|"""
             r"""(?:resource|entity|object|record|document|doc)_?[Tt]ype)""")
NAME = r"""(?<!\w)["']?""" + NAME_BODY + r"""["']?"""
LITERAL = r"""["'](?P<value>[A-Za-z0-9][\w.\-]{1,40})["']"""
FORWARD = re.compile(NAME + r"\s*(?P<op>===?|!==?|=|:|\bin\b|\.get\(|\[)\s*" + LITERAL)
REVERSE = re.compile(LITERAL + r"\s*(?:===?|!==?)\s*[\w.]*?" + NAME_BODY)
# keys that are secrets, cache or UI plumbing rather than records in the database
IGNORE_NAMES = re.compile(r"(?:api|secret|private|public|access|ssh|sort|cache|primary|foreign|storage|session|"
                          r"idempotency|partition|group|order)_?key$", re.IGNORECASE)
IGNORE_VALUES = {"true", "false", "null", "none"}
MARKER = "baton: ok"


@dataclass
class Finding:
    path: str
    line: int
    name: str
    value: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.name} = \"{self.value}\"   {self.text.strip()[:100]}"


def scan_line(text: str) -> tuple[str, str] | None:
    if MARKER in text:
        return None
    for rx in (FORWARD, REVERSE):
        for m in rx.finditer(text):
            name, value = m.group("name"), m.group("value")
            if value.lower() in IGNORE_VALUES or IGNORE_NAMES.search(name):
                continue
            # JSX: <li key="item"> is a React key, not a data key
            if name == "key" and rx is FORWARD and m.group("op") == "=":
                continue
            return name, value
    return None


def parse_added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(path, line number, text) for every added line of a `git diff -U0`."""
    out: list[tuple[str, int, str]] = []
    path, lineno = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/") if raw[4:] != "/dev/null" else ""
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            out.append((path, lineno, raw[1:]))
            lineno += 1
    return out


FORBIDDEN = "forbidden literal"


def scan_diff(diff: str, test_paths: list[str], forbid: list[re.Pattern] | None = None,
              builtin: bool = True) -> list[Finding]:
    """The built-in data-key check (source files only) and, when `forbid` is given,
    the repository's own `forbid_literals` regexes (every file that is not a test)."""
    found = []
    for path, lineno, text in parse_added_lines(diff):
        if not path or matches_any(path, test_paths) or MARKER in text:
            continue
        hit = scan_line(text) if builtin and Path(path).suffix in SOURCE_EXT else None
        if hit:
            found.append(Finding(path, lineno, hit[0], hit[1], text))
            continue
        for rx in forbid or []:
            m = rx.search(text)
            if m:
                found.append(Finding(path, lineno, FORBIDDEN, m.group(0)[:60], text))
                break
    return found


def scan_repo(repo: Path, since: str, test_paths: list[str], forbid: list[re.Pattern] | None = None,
              builtin: bool = True, started: str | None = None) -> list[Finding]:
    """Findings among the lines added since `since`. With `started` (when the task began), only the
    commits made since then count: history the task imported (a fetch, a merge, a cherry-pick) keeps its
    old commit dates, and its lines are not the task's own work."""
    if not started:
        diff = gitutil.git(repo, "diff", "-U0", "--no-color", "--no-renames", f"{since}..HEAD", check=False)
        return scan_diff(diff, test_paths, forbid, builtin)
    shas = gitutil.git(repo, "log", "--no-merges", "--reverse", f"--since={started}", "--format=%H",
                       f"{since}..HEAD", check=False).split()
    diff = "\n".join(gitutil.git(repo, "show", "-U0", "--no-color", "--no-renames", "--format=", sha, check=False)
                     for sha in shas)
    return scan_diff(diff, test_paths, forbid, builtin)


def justified(result_text: str) -> bool:
    """The agent explained its literals under a `Hardcoded keys` heading."""
    return bool(re.search(r"^#{1,6}\s*hardcoded keys\b", result_text, re.IGNORECASE | re.MULTILINE))
