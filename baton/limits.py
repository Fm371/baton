"""Classify how an agent attempt ended and, for a usage limit, when it resets.

Order of evidence, most structured first:
  1. structured events on stdout (Claude stream-json rate_limit_event, Codex
     --json JSONL) and the error type/code/status fields inside error events
     (usage_limit_exceeded, rate_limit_exceeded, insufficient_quota, 429, ...)
  2. the exit code and the result's is_error flag
  3. free text: the error text inside error events always; stderr and non-JSON
     stdout lines only when the attempt actually failed (exit code other than 0,
     or an error result), because on a clean exit they are the agent's own words
  4. the CLI's own session files (Claude can exit on a weekly limit with nothing
     on stdout; a Codex rollout may carry the reset as an epoch)

Only error-ish text is scanned, never the agent's own messages, so an agent
that writes code about "rate limits" is not mistaken for a limit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .sandbox import sandbox_error

# ---------------------------------------------------------------- outcome


@dataclass
class Outcome:
    kind: str                      # success | limit | transient | auth | sandbox | unknown
    reset_at: datetime | None = None
    window: str = ""               # five_hour | weekly | model | unknown
    model: str = ""                # window == "model": the model family that is limited (opus, sonnet, ...)
    retry_after_s: float | None = None
    source: str = ""               # which evidence decided it
    detail: str = ""               # the matching text, shortened
    session_id: str | None = None
    usage: dict = field(default_factory=dict)


# ---------------------------------------------------------------- patterns

APOS = "['’]"
LIMIT_RE = re.compile(
    r"hit your (?:[a-z0-9.\- ]{0,24})?limit"
    r"|usage[ _]limit(?:s)?[ _](?:reached|exceeded|will reset)"
    r"|reached (?:your|the) (?:usage|session|weekly)[a-z ]{0,12}limit"
    r"|(?:session|weekly|5-hour|five-hour) limit (?:reached|hit|exceeded)"
    r"|usage_limit_exceeded|usageLimitExceeded|usage_limit_reached"
    r"|out of (?:extra )?usage"
    r"|limit will reset",
    re.I,
)
# the old Claude CLI wording: the reset is an epoch after a pipe
LEGACY_EPOCH_RE = re.compile(r"limit reached\s*\|\s*(\d{9,13})\b", re.I)
WEEKLY_RE = re.compile(r"weekly|seven[_ ]day|7[- ]day", re.I)
MODEL_RE = re.compile(r"hit your (opus|sonnet|fable|haiku)\b[a-z ]{0,10}limit", re.I)
# A bare number is not evidence: `cli.js:401:9` or `line 503` in a stack trace must not read as a
# status code. A number counts only next to HTTP/status/error wording or its reason phrase.
_STATUS_CTX = r"(?:\bhttp(?:/\d(?:\.\d)?)?|\bstatus(?:[ _]code)?|\berror(?:[ _]code)?|\bcode|\bapi error)\s*[:=#(]?\s*"
TRANSIENT_RE = re.compile(
    r"rate[ _-]?limit|too many requests|overloaded"
    r"|" + _STATUS_CTX + r"(?:429|503|529)\b"
    r"|\b(?:429|503|529)\s+(?:too many requests|service unavailable|overloaded)"
    r"|temporarily unavailable|(?:at|over|exceeded|insufficient)\s+capacity|capacity\s+(?:limit|exceeded)"
    r"|server error|\b5\d\d\s+internal server error",
    re.I,
)
AUTH_RE = re.compile(
    r"not logged in|please (?:run )?/?login|invalid api key|authentication (?:failed|error|required)"
    r"|" + _STATUS_CTX + r"401\b|\b401\s+unauthori[sz]ed|\bunauthori[sz]ed\b"
    r"|oauth token (?:has )?expired|token refresh failed"
    r"|out of credits|insufficient (?:credits|quota)|credit balance is too low",
    re.I,
)
# "resets 10:10pm (Asia/Tehran)", "resets Mon 12:00am", "resets at 11am (UTC)"
APPROACH_RE = re.compile(r"approaching your [a-z0-9\- ]{0,20}limit|usage limit reached\s*[·•∙-]\s*wrapping up", re.I)
CLAUDE_RESET_RE = re.compile(
    r"\bresets?\s+(?:at\s+)?(?P<when>[^·•∙(\n]+?)\s*(?:\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-]+)*|UTC|GMT)\))?\s*(?:$|[·•∙.\n\"])",
    re.I,
)
# "try again at 1:52 PM.", "try again at Oct 9th, 2026 11:19 PM.", "try again in 2 hours"
TRY_AGAIN_RE = re.compile(r"try again (?P<prep>at|in|after) (?P<when>.+?)(?:\.(?:\s|$)|[;\n\"]|$)", re.I)
RETRY_SECONDS_RE = re.compile(r"(?:try again|retry) (?:after|in) (\d+(?:\.\d+)?) ?s(?:ec(?:ond)?s?)?\b", re.I)

# ---------------------------------------------------------------- time parsing

_ORDINAL = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)
_AMPM = re.compile(r"(\d)\s*([ap])\.?\s*m\.?(?=\W|$)", re.I)
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}

_TIME_FMTS = ["%I:%M %p", "%I %p", "%H:%M"]
_DATE_FMTS = ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%b %d", "%B %d", "%d %b", "%d %B",
              "%Y-%m-%d"]
_WEEKDAY_FMTS = ["%a", "%A"]
_ALL_FMTS: list[tuple[str, str]] = (
    [("time", t) for t in _TIME_FMTS]
    + [("weekday", f"{w} {t}") for w in _WEEKDAY_FMTS for t in _TIME_FMTS]
    + [("date", f"{d} {t}") for d in _DATE_FMTS for t in _TIME_FMTS]
    + [("date", f"{t} {d}") for d in _DATE_FMTS for t in _TIME_FMTS]
    + [("date", d) for d in _DATE_FMTS]
)


def _normalise(s: str) -> str:
    s = s.strip().rstrip(".").strip()
    s = re.sub(r"^(at|on)\s+", "", s, flags=re.I)
    s = _ORDINAL.sub(r"\1", s)
    s = s.replace(",", " ").replace(" at ", " ")
    s = _AMPM.sub(lambda m: f"{m.group(1)} {m.group(2).upper()}M", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_duration(s: str) -> timedelta | None:
    s = s.strip().rstrip(".").strip()
    s = re.sub(r"^in\s+", "", s, flags=re.I)
    rest = _DURATION_PART.sub("", s)
    if re.sub(r"[\s,]|and", "", rest, flags=re.I):
        return None
    total = 0.0
    found = False
    for num, unit in _DURATION_PART.findall(s):
        found = True
        total += float(num) * _UNIT_S[unit[0].lower()]
    return timedelta(seconds=total) if found else None


def resolve_local(day: date, h: int, mi: int, tz: tzinfo) -> datetime:
    """The aware moment of wall-clock `h:mi` on `day` in `tz`, safe across DST changes.
    A time that does not exist (the spring-forward gap) moves to the first moment after the
    gap; a time that happens twice (the fall-back hour) takes the later one, so we never retry
    an hour before the limit lifts."""
    first = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz, fold=0)
    wall = first.replace(tzinfo=None)
    settled = first.astimezone(timezone.utc).astimezone(tz)
    if settled.replace(tzinfo=None) != wall:        # in the gap
        return settled
    later = first.replace(fold=1)
    if later.utcoffset() != first.utcoffset():      # ambiguous
        return later.astimezone(timezone.utc).astimezone(tz)
    return first


_WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_GRACE = timedelta(minutes=5)       # the message is cut to the minute: a time a moment ago means "now"


def _parse_iso(raw: str, tz: tzinfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _strptime(s: str, fmt: str) -> datetime | None:
    try:
        if "%Y" in fmt:
            return datetime.strptime(s, fmt)
        # a placeholder leap year: no year-less parsing (deprecated in 3.15)
        return datetime.strptime(f"{s} 2000", f"{fmt} %Y")
    except ValueError:
        return None


def _clock_of(parsed: datetime, fmt: str) -> tuple[int, int]:
    has_time = "%H" in fmt or "%I" in fmt
    return (parsed.hour, parsed.minute) if has_time else (0, 0)


def _read_time(parsed: datetime, fmt: str, s: str, local_now: datetime, tz: tzinfo) -> datetime | None:
    """A bare time: today, or tomorrow when it passed more than five minutes ago."""
    h, mi = _clock_of(parsed, fmt)
    cand = resolve_local(local_now.date(), h, mi, tz)
    if cand < local_now - _GRACE:
        cand = resolve_local(local_now.date() + timedelta(days=1), h, mi, tz)
    return cand


def _read_weekday(parsed: datetime, fmt: str, s: str, local_now: datetime, tz: tzinfo) -> datetime | None:
    """A weekday and a time: the next such day. strptime ignores the weekday name, so read it ourselves."""
    name = s.split(" ")[0][:3].lower()
    if name not in _WEEKDAY_NAMES:
        return None
    h, mi = _clock_of(parsed, fmt)
    day = local_now.date() + timedelta(days=(_WEEKDAY_NAMES.index(name) - local_now.weekday()) % 7)
    cand = resolve_local(day, h, mi, tz)
    if cand < local_now - _GRACE:
        cand = resolve_local(day + timedelta(days=7), h, mi, tz)
    return cand


def _read_date(parsed: datetime, fmt: str, s: str, local_now: datetime, tz: tzinfo) -> datetime | None:
    """A date, with or without a year (without: this year, or next when it is long past)."""
    h, mi = _clock_of(parsed, fmt)
    year = parsed.year if "%Y" in fmt else local_now.year
    try:
        cand = resolve_local(date(year, parsed.month, parsed.day), h, mi, tz)
        if "%Y" not in fmt and cand < local_now - timedelta(days=1):
            cand = resolve_local(date(year + 1, parsed.month, parsed.day), h, mi, tz)
    except ValueError:
        return None
    return cand


_READERS = {"time": _read_time, "weekday": _read_weekday, "date": _read_date}


def parse_when(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """Turn a reset phrase into an aware datetime, or None if unreadable.

    `now` must be aware. A bare time that already passed more than five
    minutes ago means tomorrow (the message is truncated to the minute, so a
    time a moment ago means "now").
    """
    raw = text.strip()
    if not raw:
        return None
    d = parse_duration(raw)
    if d is not None:
        return now + d
    iso = _parse_iso(raw, tz)
    if iso is not None:
        return iso
    s = _normalise(raw)
    local_now = now.astimezone(tz)
    for kind, fmt in _ALL_FMTS:
        parsed = _strptime(s, fmt)
        if parsed is None:
            continue
        got = _READERS[kind](parsed, fmt, s, local_now, tz)
        if got is not None:
            return got
    return None


def _from_epoch(v) -> datetime | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e12:          # milliseconds
        v /= 1000
    if v < 1e9:
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc)


# ---------------------------------------------------------------- helpers


def _json_lines(text: str) -> tuple[list[dict], list[str]]:
    events, plain = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    events.append(obj)
                    continue
            except json.JSONDecodeError:
                pass
        plain.append(s)
    return events, plain


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _strings(obj) -> list[str]:
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_strings(v))
    return out


def _short(s: str, n: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tail_text(path: Path, max_bytes: int = 256_000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# Lines of the middle of a large output worth keeping when only its tail is read: events that carry the
# outcome, the usage and the session id. Everything else in the middle is skipped unread.
_KEEP_RE = re.compile(
    rb'rate_limit_event|codex_error_info|"type"\s*:\s*"(?:result|error|system|turn\.failed|turn\.completed|thread\.started)"')
OUTPUT_TAIL_BYTES = 4_000_000
OUTPUT_KEEP_BYTES = 4_000_000
OUTPUT_MAX_LINE = 1_000_000


def read_output(path: Path, tail_bytes: int = OUTPUT_TAIL_BYTES, keep_bytes: int = OUTPUT_KEEP_BYTES,
                max_line: int = OUTPUT_MAX_LINE) -> str:
    """An agent's output as text, with bounded memory however big the file is.

    A file up to `tail_bytes` is read whole. A larger one gives the event lines of its first part
    (result, rate-limit, error, usage and session lines, at most `keep_bytes` in all, none longer
    than `max_line`) followed by its last `tail_bytes`. The classifier only needs those."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            if size <= tail_bytes:
                f.seek(0)
                return f.read().decode("utf-8", errors="replace")
            cut = size - tail_bytes
            f.seek(0)
            kept: list[bytes] = []
            total = 0
            while f.tell() < cut:
                line = f.readline(max_line + 1)
                if not line:
                    break
                if len(line) > max_line:                     # a giant line: skip the rest of it unread
                    while not line.endswith(b"\n"):
                        line = f.readline(max_line)
                        if not line:
                            break
                    continue
                if total < keep_bytes and _KEEP_RE.search(line):
                    kept.append(line if line.endswith(b"\n") else line + b"\n")
                    total += len(line)
            f.seek(cut)
            f.readline()                                     # the tail starts on a whole line
            tail = f.read()
            return (b"".join(kept) + tail).decode("utf-8", errors="replace")
    except OSError:
        return ""


def tail_bytes_of(path: Path, n: int) -> bytes:
    """The last `n` bytes of a file, without reading the rest."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - n))
            return f.read()
    except OSError:
        return b""


def recent_files(root: Path | None, pattern: str, since: datetime, limit: int = 5) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    cutoff = since.timestamp() - 5
    found = []
    try:
        for p in root.glob(pattern):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= cutoff:
                found.append((m, p))
    except OSError:
        return []
    found.sort(reverse=True)
    return [p for _, p in found[:limit]]


# Error type/code names that say what happened without any wording to parse.
_CODE_KINDS = {
    "usage_limit_exceeded": "limit", "usagelimitexceeded": "limit", "usage_limit_reached": "limit",
    "insufficient_quota": "auth", "billing_hard_limit_reached": "auth",
    "authentication_error": "auth", "invalid_api_key": "auth", "permission_error": "auth",
    "rate_limit_exceeded": "transient", "rate_limit_error": "transient", "overloaded_error": "transient",
}
_STATUS_KINDS = {401: "auth", 429: "transient", 503: "transient", 529: "transient"}
_CODE_FIELDS = ("type", "code", "error_code", "error_type", "codex_error_info", "status", "status_code")
_KIND_RANK = {"limit": 0, "auth": 1, "transient": 2}


def _code_kind(events: list[dict]) -> str | None:
    """Kind named by error type/code/status fields inside error events, or None.
    Most decisive wins: limit, then auth, then transient."""
    best: str | None = None

    def offer(kind: str | None) -> None:
        nonlocal best
        if kind and (best is None or _KIND_RANK[kind] < _KIND_RANK[best]):
            best = kind

    for ev in events:
        for d in _walk(ev):
            for k, v in d.items():
                if isinstance(k, str):
                    offer(_CODE_KINDS.get(k.lower()))     # codex_error_info: {"usage_limit_exceeded": {}}
                if k not in _CODE_FIELDS:
                    continue
                if isinstance(v, str):
                    offer(_CODE_KINDS.get(v.lower()))
                elif isinstance(v, int) and not isinstance(v, bool):
                    offer(_STATUS_KINDS.get(v))
    return best


def _apply_code(out: Outcome | None, code: str | None, source: str) -> Outcome | None:
    """Let a structured code decide the kind; keep the reset time the text grammar found."""
    if code is None or (out is not None and out.kind == code):
        return out
    if code == "limit":
        reset = out.reset_at if out is not None and out.kind == "limit" else None
        return Outcome("limit", reset_at=reset, window="unknown", source=source, detail="usage_limit_exceeded")
    if out is not None and out.kind == "limit":
        return out                        # wording found a limit the code did not name
    return Outcome(code, source=source, detail=out.detail if out is not None and out.kind == code else code)


def _limit_window(joined: str) -> tuple[str, str]:
    """(window, model) a limit message names: weekly, a model's own limit, or a five-hour session."""
    window = "weekly" if WEEKLY_RE.search(joined) else "five_hour"
    mm = MODEL_RE.search(joined)
    if mm:
        return "model", mm.group(1).lower()
    if window == "five_hour" and not re.search(r"session|5-hour|five[_ -]hour", joined, re.I):
        window = "unknown"   # decided later from the distance to the reset
    return window, ""


def _limit_reset(joined: str, now: datetime, tz: tzinfo, provider: str) -> datetime | None:
    reset = None
    # Prefer the phrase nearest to the limit message
    for line in joined.splitlines():
        if not (LIMIT_RE.search(line) or "reset" in line.lower() or "try again" in line.lower()):
            continue
        reset = reset or _reset_from_line(line, now, tz, provider)
    if reset is None:
        legacy = LEGACY_EPOCH_RE.search(joined)       # "Claude AI usage limit reached|1760000000"
        if legacy:
            reset = _from_epoch(legacy.group(1))
    return reset


def _excerpt(joined: str, m: re.Match, before: int, after: int) -> str:
    return _short(joined[max(0, m.start() - before): m.end() + after])


def _classify_text(texts: list[str], now: datetime, tz: tzinfo, provider: str, source: str) -> Outcome | None:
    """Apply the text grammar. Returns a limit/transient/auth outcome or None."""
    joined = "\n".join(t for t in texts if t)
    if not joined.strip():
        return None
    m = LIMIT_RE.search(joined)
    if m:
        window, model = _limit_window(joined)
        return Outcome("limit", reset_at=_limit_reset(joined, now, tz, provider), window=window, model=model,
                       source=source, detail=_excerpt(joined, m, 80, 120))
    a = AUTH_RE.search(joined)
    if a:
        return Outcome("auth", source=source, detail=_excerpt(joined, a, 60, 80))
    t = TRANSIENT_RE.search(joined)
    if t:
        r = RETRY_SECONDS_RE.search(joined)
        return Outcome("transient", retry_after_s=float(r.group(1)) if r else None, source=source,
                       detail=_excerpt(joined, t, 60, 80))
    return None


def _classify_layers(event_texts: list[str], fallback_texts: list[str], now: datetime, tz: tzinfo,
                     provider: str, source: str) -> Outcome | None:
    """Error text inside structured events decides first. stderr and plain stdout lines (stack traces,
    debug output) are looked at only when the events say nothing, so a number or word in a trace never
    overrules what the CLI reported in its own events."""
    return (_classify_text(event_texts, now, tz, provider, source)
            or _classify_text(fallback_texts, now, tz, provider, source))


def _reset_from_line(line: str, now: datetime, tz: tzinfo, provider: str) -> datetime | None:
    for m in TRY_AGAIN_RE.finditer(line):
        when = m.group("when")
        if m.group("prep").lower() in ("in", "after"):
            d = parse_duration(when)
            if d is not None:
                return now + d
        r = parse_when(when, now, tz)
        if r:
            return r
    for m in CLAUDE_RESET_RE.finditer(line):
        zone = tz
        if m.group("tz"):
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                zone = timezone.utc if m.group("tz").upper() in ("UTC", "GMT") else ZoneInfo(m.group("tz"))
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                zone = tz
        r = parse_when(m.group("when"), now, zone)
        if r:
            return r
    return None


def _fix_window(o: Outcome, now: datetime) -> Outcome:
    if o.kind == "limit" and o.reset_at is not None:
        if o.window in ("unknown", "five_hour") and o.reset_at - now > timedelta(hours=6):
            o.window = "weekly"
        if o.window == "unknown":
            o.window = "five_hour"
    return o


def _apply_error_code(out: Outcome | None, err_events: list[dict], recovered: bool,
                      source: str) -> Outcome | None:
    """Let the error type/code/status fields in the CLI's own events decide the kind. A transient code
    on an attempt that went on to succeed was retried inside the CLI: it counts for nothing."""
    code = _code_kind(err_events)
    return _apply_code(out, None if code == "transient" and recovered else code, source)


def _drop_recovered(out: Outcome | None, recovered: bool) -> Outcome | None:
    """A transient outcome on an attempt that went on to succeed was retried inside the CLI."""
    return None if out is not None and out.kind == "transient" and recovered else out


def _exit_outcome(source: str, exit_code: int | None, recovered: bool) -> Outcome:
    """Nothing in the output said more than the exit code."""
    return Outcome("success" if recovered else "unknown", source=source, detail=f"exit {exit_code}")


# ---------------------------------------------------------------- Claude

_RESET_KEYS = ("resetsAt", "resets_at", "resetAt")
_USAGE_EXTRAS = ("total_cost_usd", "num_turns", "duration_ms")
_RESULT_ERROR_KEYS = ("result", "error", "errors", "api_error", "terminal_reason", "subtype")


@dataclass
class _ClaudeScan:
    """What one pass over a Claude stream-json output found."""
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)          # error text inside events
    err_events: list[dict] = field(default_factory=list)    # error events, for their type/code/status fields
    structured: Outcome | None = None                       # a rate_limit_event that rejected the request
    result_error: bool = False
    warning_reset: datetime | None = None                   # the reset an "allowed_warning" announced
    last_text: str = ""


def _reset_epoch(info: dict) -> datetime | None:
    """The first reset epoch (seconds or milliseconds) found anywhere in a rate-limit record."""
    for d in _walk(info):
        for k in _RESET_KEYS:
            if k in d:
                reset = _from_epoch(d[k])
                if reset is not None:
                    return reset
    return None


def _rate_limit_window(rtype: str) -> tuple[str, str]:
    """(window, model family) of a rate-limit type such as five_hour, seven_day or seven_day_opus."""
    window = "weekly" if WEEKLY_RE.search(rtype) else ("five_hour" if rtype else "unknown")
    fam = re.search(r"opus|sonnet|fable|haiku", rtype, re.I)
    return ("model", fam.group(0).lower()) if fam else (window, "")


def _claude_rate_limit(ev: dict, scan: _ClaudeScan) -> None:
    info = ev.get("rate_limit_info") if isinstance(ev.get("rate_limit_info"), dict) else ev
    status = str(info.get("status", "")).lower()
    reset = _reset_epoch(info)
    rtype = str(info.get("rateLimitType") or info.get("rate_limit_type") or "")
    if status.startswith("allowed_warning") and reset:
        scan.warning_reset = reset
    if status and not status.startswith("allowed"):
        window, model = _rate_limit_window(rtype)
        scan.structured = Outcome("limit", reset_at=reset, window=window, model=model,
                                  source="claude:rate_limit_event", detail=_short(json.dumps(info)))


def _claude_usage(ev: dict) -> dict:
    usage = dict(ev["usage"])
    for k in _USAGE_EXTRAS:
        if k in ev:
            usage[k] = ev[k]
    return usage


def _claude_result(ev: dict, scan: _ClaudeScan) -> None:
    if isinstance(ev.get("result"), str):
        scan.last_text = ev["result"]
    if isinstance(ev.get("usage"), dict):
        scan.usage = _claude_usage(ev)
    if ev.get("is_error") or str(ev.get("subtype", "")).startswith("error"):
        scan.result_error = True
        keys = _RESULT_ERROR_KEYS
        if str(ev.get("subtype", "")).startswith("error_max"):
            # the turn/budget cap ended the run: `result` is then the agent's own last words
            keys = tuple(k for k in keys if k != "result")
        body = {k: ev.get(k) for k in keys}
        scan.err_events.append(body)
        scan.texts.extend(_strings(body))


def _claude_error(ev: dict, etype, scan: _ClaudeScan) -> None:
    if etype == "system" and ev.get("subtype") == "init":
        return
    body = {k: v for k, v in ev.items() if k not in ("session_id", "uuid")}
    scan.err_events.append(body)
    scan.texts.extend(_strings(body))


def _scan_claude(events: list[dict]) -> _ClaudeScan:
    scan = _ClaudeScan()
    for ev in events:
        if scan.session_id is None and isinstance(ev.get("session_id"), str):
            scan.session_id = ev["session_id"]
        etype = ev.get("type")
        if etype == "rate_limit_event":
            _claude_rate_limit(ev, scan)
        elif etype == "result":
            _claude_result(ev, scan)
        elif etype in ("error", "system") or "error" in ev:
            _claude_error(ev, etype, scan)
    return scan


def _claude_transcript_limit(claude_home: Path | None, since: datetime, now: datetime,
                             tz: tzinfo) -> Outcome | None:
    """Known bug: a weekly limit can exit 1 with nothing useful on stdout; the CLI's own session
    files then carry the message."""
    for p in recent_files(claude_home / "projects" if claude_home else None, "*/*.jsonl", since):
        ev2, _ = _json_lines(_tail_text(p))
        err_texts: list[str] = []
        for ev in ev2[-60:]:
            if ev.get("error") or ev.get("isApiErrorMessage") or ev.get("type") == "system":
                err_texts.extend(_strings(ev.get("message", ev)))
        got = _classify_text(err_texts, now, tz, "claude", f"claude:transcript:{p.name}")
        if got and got.kind == "limit":
            return got
    return None


def _merge_structured(out: Outcome | None, structured: Outcome | None) -> Outcome | None:
    """A rate_limit_event that rejected the request wins; it borrows the reset the text found."""
    if not structured:
        return out
    if out and out.kind == "limit" and structured.reset_at is None:
        structured.reset_at = out.reset_at
    return structured


def _claude_late_evidence(scan: _ClaudeScan, exit_code: int | None, claude_home: Path | None,
                          since: datetime, now: datetime, tz: tzinfo) -> Outcome | None:
    """Nothing else decided: the result's own wrap-up warning, then (after a failure) the CLI's session files."""
    if APPROACH_RE.search(scan.last_text):
        return Outcome("limit", reset_at=scan.warning_reset, window="five_hour", source="claude:wrap-up",
                       detail=_short(scan.last_text[-200:]))
    if exit_code not in (0, None) or scan.result_error:
        return _claude_transcript_limit(claude_home, since, now, tz)
    return None


def classify_claude(stdout: str, stderr: str, exit_code: int | None, now: datetime, tz: tzinfo,
                    claude_home: Path | None = None, started: datetime | None = None) -> Outcome:
    events, plain = _json_lines(stdout)
    scan = _scan_claude(events)
    recovered = exit_code == 0 and not scan.result_error      # a transient error the CLI retried through
    # stderr and plain stdout are only evidence of a failure when the attempt failed;
    # on a clean exit they are the agent's own words
    fallback = [] if recovered else [*plain, stderr]
    out = _classify_layers(scan.texts, fallback, now, tz, "claude", "claude:text")
    out = _apply_error_code(out, scan.err_events, recovered, "claude:code")
    out = _drop_recovered(_merge_structured(out, scan.structured), recovered)
    if out is None:
        out = _claude_late_evidence(scan, exit_code, claude_home, started or now, now, tz)
    if out is None:
        out = _exit_outcome("claude:exit", exit_code, recovered)
    out.session_id = scan.session_id
    out.usage = scan.usage
    return _fix_window(out, now)


# ---------------------------------------------------------------- Codex

_LIMIT_MARKERS = ("usage_limit_exceeded", "usageLimitExceeded")
_ROLLOUT_EVENTS = 80       # how much of a rollout's tail is read for the limit and its reset


@dataclass
class _CodexScan:
    """What one pass over a Codex `--json` output found."""
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)          # strings inside error events
    err_events: list[dict] = field(default_factory=list)    # error events, for their type/code/status fields
    flagged: bool = False                                   # an error event names a usage limit
    failed: bool = False                                    # an `error` or `turn.failed` event was seen


def _codex_add_usage(usage: dict, ev: dict) -> None:
    for k, v in ev["usage"].items():
        if isinstance(v, (int, float)):
            usage[k] = usage.get(k, 0) + v


def _codex_is_error(ev: dict, etype: str) -> bool:
    return etype in ("error", "turn.failed") or etype.endswith(".failed") or "codex_error_info" in json.dumps(ev)


def _scan_codex(events: list[dict]) -> _CodexScan:
    scan = _CodexScan()
    for ev in events:
        etype = str(ev.get("type", ""))
        if etype == "thread.started" and scan.session_id is None:
            scan.session_id = ev.get("thread_id") or ev.get("session_id")
        if etype == "turn.completed" and isinstance(ev.get("usage"), dict):
            _codex_add_usage(scan.usage, ev)
        if _codex_is_error(ev, etype):
            scan.failed = scan.failed or etype in ("error", "turn.failed")
            strings = _strings(ev)
            scan.err_events.append(ev)
            scan.texts.extend(strings)
            if any(m in x for x in strings for m in _LIMIT_MARKERS):
                scan.flagged = True
    return scan


def _rollout_reset(events: list[dict], now: datetime) -> datetime | None:
    """The reset of a rate-limit window that is used up (>= 100 %), latest event first."""
    reset = None
    for e in reversed(events):
        for d in _walk(e):
            if "used_percent" in d and float(d.get("used_percent") or 0) >= 100:
                r = _from_epoch(d.get("resets_at"))
                if r is None and "resets_in_seconds" in d:
                    r = now + timedelta(seconds=float(d["resets_in_seconds"]))
                if r and r > now and (reset is None or r > reset):
                    reset = r
        if reset:
            break
    return reset


def _rollout_outcome(events: list[dict], name: str, now: datetime, tz: tzinfo) -> Outcome:
    reset = _rollout_reset(events, now)
    err: list[str] = []
    for e in events:
        if "usage_limit_exceeded" in json.dumps(e) or e.get("type") == "error":
            err.extend(_strings(e))
    got = _classify_text(err, now, tz, "codex", f"codex:rollout:{name}") or Outcome(
        "limit", source=f"codex:rollout:{name}", detail="usage_limit_exceeded")
    if got.kind == "limit" and got.reset_at is None:
        got.reset_at = reset
    return got


def _codex_rollout(out: Outcome | None, codex_home: Path | None, since: datetime, now: datetime,
                   tz: tzinfo) -> Outcome | None:
    """Rollout files carry codex_error_info and rate-limit windows with resets_at: use the newest
    one that shows a usage limit, to decide the outcome or to fill in a missing reset."""
    home = codex_home / "sessions" if codex_home else None
    for p in recent_files(home, "**/rollout-*.jsonl", since):
        ev2, _ = _json_lines(_tail_text(p))
        tail = ev2[-_ROLLOUT_EVENTS:]
        if not any("usage_limit_exceeded" in json.dumps(e) for e in tail):
            continue
        got = _rollout_outcome(tail, p.name, now, tz)
        if out is None or out.kind != "limit":
            return got
        if out.reset_at is None:
            out.reset_at = got.reset_at
        return out
    return out


def _wants_rollout(out: Outcome | None, scan: _CodexScan, exit_code: int | None) -> bool:
    """Look in the rollout files when the output explains nothing about a failure, or a limit has no reset."""
    if out is None:
        return scan.flagged or scan.failed or exit_code not in (0, None)
    return out.kind == "limit" and out.reset_at is None


def _sandbox_override(out: Outcome | None, stdout: str, stderr: str) -> Outcome | None:
    """A broken sandbox means no command ran, whatever the agent concluded (a limit still wins)."""
    broken = sandbox_error(stdout, stderr)
    if broken and (out is None or out.kind != "limit"):
        return Outcome("sandbox", source="codex:sandbox", detail=broken)
    return out


CREDITS_RE = re.compile(r"out of credits|insufficient (?:credits|quota)|credit balance is too low"
                        r"|credits[_ ]depleted", re.I)
CREDITS_RETRY = timedelta(hours=1)    # when Codex names no reset: look again hourly


def _any_future_reset(events: list[dict], now: datetime) -> datetime | None:
    """The nearest future `resets_at` in any rate-limit window the events carry."""
    for e in reversed(events):
        found = []
        for d in _walk(e):
            r = _from_epoch(d.get("resets_at")) if "resets_at" in d else None
            if r is not None and r > now:
                found.append(r)
        if found:
            return min(found)
    return None


def _credits_as_limit(out: Outcome | None, events: list[dict], now: datetime, tz: tzinfo,
                      codex_home: Path | None, since: datetime) -> Outcome | None:
    """Codex "out of credits" is a usage limit that lifts at a reset, not a login problem: disabling the
    provider for the whole run would leave Codex-only tasks blocked for good. The reset comes from
    Codex's own rate-limit event, else its rollout, else an hourly retry."""
    if out is None or out.kind != "auth" or not CREDITS_RE.search(out.detail or ""):
        return out
    reset = _rollout_reset(events, now) or _any_future_reset(events, now)
    if reset is None:
        got = _codex_rollout(None, codex_home, since, now, tz)
        reset = got.reset_at if got is not None and got.kind == "limit" else None
    return Outcome("limit", reset_at=reset or now + CREDITS_RETRY, window="unknown",
                   source="codex:credits", detail=out.detail)


def classify_codex(stdout: str, stderr: str, exit_code: int | None, now: datetime, tz: tzinfo,
                   codex_home: Path | None = None, started: datetime | None = None) -> Outcome:
    events, plain = _json_lines(stdout)
    scan = _scan_codex(events)
    recovered = exit_code == 0 and not scan.failed
    fallback = [] if recovered else [*plain, stderr]      # on a clean exit these are the agent's own words
    out = _classify_layers(scan.texts, fallback, now, tz, "codex", "codex:text")
    out = _drop_recovered(_apply_error_code(out, scan.err_events, recovered, "codex:code"), recovered)
    out = _credits_as_limit(out, events, now, tz, codex_home, started or now)
    if _wants_rollout(out, scan, exit_code):
        out = _codex_rollout(out, codex_home, started or now, now, tz)
    if out is None and scan.flagged:
        out = Outcome("limit", source="codex:codex_error_info", detail="usage_limit_exceeded")
    out = _sandbox_override(out, stdout, stderr)
    if out is None:
        out = _exit_outcome("codex:exit", exit_code, recovered)
    out.session_id = scan.session_id
    out.usage = scan.usage
    return _fix_window(out, now)


def classify(provider: str, *args, **kwargs) -> Outcome:
    if provider == "claude":
        return classify_claude(*args, **kwargs)
    if provider == "codex":
        return classify_codex(*args, **kwargs)
    raise ValueError(provider)


UNMATCHED_NOTE = ("no limit pattern matched; if this was a usage limit, "
                  "the message format may have changed")


def unmatched_failure_warning(provider: str, outcome: Outcome, stdout: str, stderr: str,
                              exit_code: int | None) -> str | None:
    """A log line for a run that failed without any known pattern matching, else None.

    `classify` returns kind "unknown" for that case; the caller logs this text so a
    changed CLI message format is visible instead of looking like a plain crash."""
    if outcome.kind != "unknown":
        return None
    tail = " | ".join(x.strip() for x in (stderr or "", stdout or "") if x.strip())
    tail = " ".join(tail.split())
    excerpt = _short(tail[-300:], 200) if tail else "(no output)"
    return f"{provider}: exit {exit_code}: {UNMATCHED_NOTE}. Output excerpt: {excerpt}"
