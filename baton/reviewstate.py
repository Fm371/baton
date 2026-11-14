"""Cross-provider review: the review file format, its verdict, and the labels `baton plan` and
`baton status` show. Pure functions; nothing here touches git or runs an agent.

A review is a Markdown file, reviews/<task id>.md, whose first line is `VERDICT: approve|changes|reject`.
Every finding after it is one line that starts with a tag:

    [blocker] src/app.py:42 — the total is computed before the discount is applied
    [should]  src/app.py:57 — the helper is called twice
    [nit]     README.md:3 — typo

Parsing is strict about the shape (the first non-empty line must be the verdict, a finding must carry a
tag) and lenient only about case and whitespace. A review that does not parse is kept as it was written
and marked `invalid`; it sets no verdict."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VERDICTS = ("approve", "changes", "reject")
SEVERITIES = ("blocker", "should", "nit")
INVALID = "invalid"

VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(approve|changes|reject)\s*$", re.IGNORECASE)
FINDING_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?\[(blocker|should|nit)\]\s*(.*?)\s*$", re.IGNORECASE)
LOCATION_RE = re.compile(r"^(\S+?:\d+(?:-\d+)?)\b[\s:,—–-]*(.*)$")
SEPARATORS = " \t—–-:"


@dataclass
class Finding:
    severity: str            # blocker | should | nit
    location: str            # path:line when the reviewer gave one, else ""
    reason: str
    text: str = ""           # the line as written, without its bullet

    def label(self) -> str:
        where = f" {self.location}" if self.location else ""
        return f"[{self.severity}]{where} — {self.reason}".rstrip(" —")


@dataclass
class ParsedReview:
    verdict: str | None = None
    findings: list[Finding] = field(default_factory=list)
    problem: str = ""        # why it is invalid, when it is

    @property
    def valid(self) -> bool:
        return self.verdict is not None

    def of(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


def parse_finding(line: str) -> Finding | None:
    m = FINDING_RE.match(line)
    if not m:
        return None
    body = m.group(2)
    loc = LOCATION_RE.match(body)
    location, reason = (loc.group(1), loc.group(2)) if loc else ("", body)
    return Finding(m.group(1).lower(), location, reason.strip(SEPARATORS), line.strip())


def parse_review(text: str) -> ParsedReview:
    """The verdict and findings of a review text. `verdict` is None when the first non-empty line is not
    a `VERDICT:` line."""
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return ParsedReview(problem="the review is empty")
    m = VERDICT_RE.match(lines[first])
    if not m:
        return ParsedReview(problem="the first line is not `VERDICT: approve|changes|reject`")
    found = [f for f in (parse_finding(ln) for ln in lines[first + 1:]) if f]
    return ParsedReview(verdict=m.group(1).lower(), findings=found)


def normalised(text: str) -> str:
    """A valid review as saved: no blank lines before the VERDICT line, one newline at the end. Nothing else
    is rewritten; text before the verdict line makes a review invalid (see parse_review)."""
    return text.strip() + "\n"


def reviews_dir(cfg) -> Path:
    return cfg.base_dir / "reviews"


def review_path(cfg, task_id: str) -> Path:
    return reviews_dir(cfg) / f"{task_id}.md"


def read_review(cfg, task_id: str) -> ParsedReview | None:
    """The parsed review of a task, or None when there is no review file."""
    try:
        return parse_review(review_path(cfg, task_id).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# ---------------------------------------------------------------- what plan and status show


def review_label(task, ts) -> str:
    """none | pending | approve | changes | reject | invalid. `pending`: the task asked for a review
    (review = true), is done, and has none yet."""
    if ts.review_verdict:
        return ts.review_verdict
    if task.review and ts.status == "done":
        return "pending"
    return "none"


def fix_label(ts) -> str:
    """not run | done | blocked | interrupted (a fix that started and never finished)."""
    return {"done": "done", "blocked": "blocked", "running": "interrupted"}.get(ts.fix_state, "not run")


def reviewable(task) -> bool:
    return task.runs_agent and not task.is_verifier and not task.is_approval


def review_lines(tasks, state) -> list[str]:
    """One line per task that has anything to say about review: `review = true`, a review, or a fix."""
    rows = []
    for t in tasks:
        ts = state.task(t.id)
        if not reviewable(t) or not (t.review or ts.review_verdict or ts.fix_round or ts.fix_state):
            continue
        by = f" by {ts.review_by}" if ts.review_by else ""
        rows.append(f"  {t.id}: review {review_label(t, ts)}{by if ts.review_verdict else ''}, "
                    f"fix {fix_label(ts)}" + (f" (round {ts.fix_round})" if ts.fix_round else ""))
    return rows


def review_report(tasks, state) -> list[str]:
    """The block `baton plan` and `baton status` print; empty when no task is involved."""
    rows = review_lines(tasks, state)
    return ["reviews (opt-in: `baton review`, `baton review-fix`; never run by `baton run`):", *rows] if rows else []
