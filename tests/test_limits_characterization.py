"""Characterization of baton.limits: a large generated corpus is run through the classifiers and compared
with a recorded snapshot, so a refactor of the classifier code cannot change any output field.

The corpus is every limit fixture (as stdout and as stderr, for both providers, several exit codes and
zones) plus synthetic event streams built from the shapes the classifiers read (Claude stream-json,
Codex JSONL, error codes, stack traces, session files). The snapshot (hashes of case ids and answers, to
keep the file small) is `tests/fixtures/limits_characterization.json`. Regenerate it only when the behaviour is meant to change:

    python3 -m tests.test_limits_characterization --write
"""

import hashlib
import itertools
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from baton.limits import classify_claude, classify_codex, parse_when

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "limits"
SNAPSHOT = HERE / "fixtures" / "limits_characterization.json"

UTC = timezone.utc
TEH = ZoneInfo("Asia/Tehran")
BER = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TEH)
NOW2 = datetime(2026, 10, 24, 23, 52, tzinfo=BER)
STARTED = NOW - timedelta(minutes=30)
EXIT_CODES = (0, 1, None, 2)


def jl(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


# ------------------------------------------------------------------ event pieces

CLAUDE_PIECES = {
    "init": {"type": "system", "subtype": "init", "session_id": "s-1"},
    "res-ok": {"type": "result", "subtype": "success", "is_error": False, "session_id": "s-2",
               "result": "done, all good", "usage": {"input_tokens": 5, "output_tokens": 7},
               "total_cost_usd": 0.5, "num_turns": 3, "duration_ms": 1200},
    "res-session": {"type": "result", "is_error": True,
                    "result": "You've hit your session limit · resets 10:10pm (Asia/Tehran)"},
    "res-weekly": {"type": "result", "is_error": True,
                   "result": "You’ve hit your weekly limit ∙ resets Mon 12:00am (UTC)"},
    "res-opus": {"type": "result", "is_error": True,
                 "result": "You've hit your Opus limit · resets Oct 27, 9am (Europe/Berlin)"},
    "res-sonnet": {"type": "result", "is_error": True, "result": "You've hit your sonnet usage limit"},
    "res-noreset": {"type": "result", "is_error": True, "result": "You've hit your limit"},
    "res-legacy": {"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1792837800"},
    "res-auth": {"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"},
    "res-401": {"type": "result", "is_error": True, "result": "API Error: 401 unauthorized"},
    "res-429": {"type": "result", "is_error": True, "result": "API Error: 429 Too Many Requests, retry after 30s"},
    "res-overload": {"type": "result", "is_error": True, "result": "overloaded_error: Overloaded"},
    "res-trace": {"type": "result", "is_error": True, "result": "Error at cli.js:401:9 near line 503"},
    "res-maxturns": {"type": "result", "subtype": "error_max_turns", "is_error": False,
                     "result": "usage limit exceeded 429", "num_turns": 9},
    "res-error-sub": {"type": "result", "subtype": "error_during_execution", "is_error": False,
                      "error": "something exploded", "errors": ["e1", "e2"]},
    "res-wrapup": {"type": "result", "is_error": False,
                   "result": "Approaching your limit · usage limit reached · wrapping up"},
    "res-talk": {"type": "result", "is_error": False,
                 "result": "I added a retry for 'usage limit reached' and rate limit handling"},
    "rl-warn": {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": 1792837800, "rateLimitType": "five_hour"}},
    "rl-allowed": {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1793000000}},
    "rl-rej-5h": {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1792837800, "rateLimitType": "five_hour"}},
    "rl-rej-week": {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1793400000000, "rateLimitType": "seven_day"}},
    "rl-rej-opus": {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resets_at": 1792837800, "rate_limit_type": "seven_day_opus"}},
    "rl-rej-noreset": {"type": "rate_limit_event", "status": "blocked", "rateLimitType": ""},
    "err-event": {"type": "error", "error": {"message": "boom", "type": "server_error"}},
    "err-limit-code": {"type": "error", "error": {"type": "usage_limit_exceeded", "message": "denied"}},
    "err-quota": {"type": "error", "error": {"code": "insufficient_quota"}},
    "err-401-code": {"type": "error", "status": 401},
    "err-503-code": {"type": "error", "status_code": 503, "message": "x"},
    "err-rate-code": {"type": "error", "error": {"type": "rate_limit_error"}},
    "err-auth-code": {"type": "error", "error": {"type": "authentication_error"}},
    "sys-error": {"type": "system", "subtype": "api_retry", "error": "rate limit hit"},
    "assistant": {"type": "assistant", "message": {"content": [{"type": "text", "text": "usage limit exceeded"}]}},
    "bool-status": {"type": "error", "status": True},
}

CODEX_PIECES = {
    "thread": {"type": "thread.started", "thread_id": "th-1"},
    "thread-sid": {"type": "thread.started", "session_id": "th-2"},
    "usage": {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4, "note": "x"}},
    "usage2": {"type": "turn.completed", "usage": {"input_tokens": 3, "cached": 1.5}},
    "item": {"type": "item.completed", "item": {"type": "agent_message", "text": "usage limit exceeded in tests"}},
    "failed-clock": {"type": "turn.failed", "error": {
        "message": "You've hit your usage limit. Try again at 1:52 PM."}},
    "failed-date": {"type": "turn.failed", "error": {
        "message": "You've hit your usage limit. Try again at Oct 27th, 2026 9:05 AM."}},
    "failed-rel": {"type": "turn.failed", "error": {"message": "usage limit reached, try again in 1 hour 5 minutes"}},
    "failed-noreset": {"type": "turn.failed", "error": {"message": "usage limit exceeded"}},
    "failed-429": {"type": "turn.failed", "error": {"message": "429 Too Many Requests, retry after 60.5s"}},
    "failed-auth": {"type": "turn.failed", "error": {"message": "401 Unauthorized: token refresh failed"}},
    "failed-plain": {"type": "turn.failed", "error": {"message": "boom"}},
    "error-info": {"type": "error", "codex_error_info": {"usage_limit_exceeded": {}}, "message": "stopped"},
    "error-info-camel": {"type": "error", "message": "usageLimitExceeded"},
    "error-quota": {"type": "error", "error": {"code": "insufficient_quota"}},
    "error-429": {"type": "error", "status": 429},
    "error-rate": {"type": "error", "error": {"type": "rate_limit_exceeded", "message": "slow down"}},
    "x-failed": {"type": "item.failed", "item": {"message": "tool died"}},
    "info-only": {"type": "item.completed", "codex_error_info": "other", "note": "x"},
}

PLAIN_TEXTS = {
    "none": "",
    "limit-line": "You've hit your usage limit. Try again at 1:52 PM.",
    "trace-401": "    at run (/x/cli.js:401:9)\n    at line 503",
    "http-503": "Error: HTTP 503 Service Unavailable",
    "auth-line": "Error: not logged in",
    "bwrap": "bwrap: Can't create user namespace: Operation not permitted",
    "bwrap-limit": "bwrap: Operation not permitted\nYou've hit your usage limit. Try again at 1:52 PM.",
    "weekly-line": "You've hit your weekly limit, resets Mon 9:00 AM (Europe/Berlin)",
    "talk": "I will add a rate limit handler",
    "capacity": "The server is at capacity right now",
}


# ------------------------------------------------------------------ cases


def _fixture_cases():
    for p in sorted(FIX.glob("*.txt")):
        text = p.read_text(encoding="utf-8")
        for provider, code, where, tz, now in itertools.product(
                ("claude", "codex"), EXIT_CODES, ("out", "err"), (UTC, TEH), (NOW, NOW2)):
            yield (f"fx:{p.name}:{provider}:{code}:{where}:{tz.key if hasattr(tz, 'key') else 'utc'}:"
                   f"{now.tzinfo.key}"), provider, (text, "") if where == "out" else ("", text), code, tz, now, None


def _piece_cases():
    c_names, x_names = sorted(CLAUDE_PIECES), sorted(CODEX_PIECES)
    singles = [(n,) for n in c_names]
    pairs = list(itertools.combinations(c_names, 2))      # every pair: events interact (a warning, then a wrap-up)
    triples = list(itertools.combinations(c_names, 3))[::53]
    for combo in singles + pairs + triples:
        stdout = jl(*[CLAUDE_PIECES[n] for n in combo])
        for code, plain, tz in itertools.product((0, 1, None), ("none", "limit-line", "trace-401"), (UTC, TEH)):
            if len(combo) == 2 and (plain != "none" or code is None or tz is UTC):
                continue
            if len(combo) > 2 and plain != "none":
                continue
            yield (f"cl:{'+'.join(combo)}:{code}:{plain}:{tz.key if hasattr(tz, 'key') else 'utc'}",
                   "claude", (stdout, PLAIN_TEXTS[plain]), code, tz, NOW, None)
    xs = [(n,) for n in x_names] + list(itertools.combinations(x_names, 2)) + \
        list(itertools.combinations(x_names, 3))[::47]
    for combo in xs:
        stdout = jl(*[CODEX_PIECES[n] for n in combo])
        for code, plain, tz in itertools.product((0, 1, None), tuple(PLAIN_TEXTS), (UTC, TEH)):
            if len(combo) == 2 and (plain not in ("none", "bwrap") or code is None or tz is UTC):
                continue
            if len(combo) > 2 and plain not in ("none", "bwrap"):
                continue
            yield (f"cx:{'+'.join(combo)}:{code}:{plain}:{tz.key if hasattr(tz, 'key') else 'utc'}",
                   "codex", (stdout, PLAIN_TEXTS[plain]), code, tz, NOW, None)
    for provider, code, plain in itertools.product(("claude", "codex"), EXIT_CODES, tuple(PLAIN_TEXTS)):
        yield f"plain:{provider}:{code}:{plain}", provider, (PLAIN_TEXTS[plain], ""), code, UTC, NOW, None
        yield f"plain-err:{provider}:{code}:{plain}", provider, ("", PLAIN_TEXTS[plain]), code, TEH, NOW, None


def _session_cases():
    """Cases whose answer depends on the CLI's own session files."""
    claude_files = {
        "weekly": [{"type": "assistant", "isApiErrorMessage": True,
                    "message": {"content": [{"type": "text",
                                             "text": "You've hit your weekly limit · resets Mon 12:00am (UTC)"}]}}],
        "session": [{"error": "x", "message": "You've hit your session limit · resets 10:10pm (Asia/Tehran)"}],
        "system": [{"type": "system", "message": "usage limit reached, try again in 2 hours"}],
        "auth": [{"error": "invalid api key"}],
        "chat": [{"type": "user", "message": "talking about the usage limit"}],
    }
    codex_files = {
        "limit-reset-epoch": [{"type": "event_msg", "payload": {"rate_limits": {"primary": {
            "used_percent": 100.0, "resets_at": 1792837800}}}},
            {"type": "error", "message": "usage_limit_exceeded"}],
        "limit-reset-in": [{"rate": {"used_percent": 100, "resets_in_seconds": 7200}},
                           {"codex_error_info": "usage_limit_exceeded"}],
        "limit-two-windows": [{"p": {"used_percent": 100, "resets_at": 1792837800}},
                              {"s": {"used_percent": 100, "resets_at": 1793442600}},
                              {"type": "error", "message": "usage_limit_exceeded"}],
        "limit-text": [{"type": "error", "message": "You've hit your usage limit. Try again at 1:52 PM."}],
        "under": [{"p": {"used_percent": 40, "resets_at": 1792837800}},
                  {"type": "error", "message": "usage_limit_exceeded"}],
        "none": [{"type": "event_msg", "payload": {}}],
    }
    stdouts = {
        "empty": "",
        "claude-fail": jl(CLAUDE_PIECES["res-noreset"]),
        "claude-ok": jl(CLAUDE_PIECES["res-ok"]),
        "claude-auth": jl(CLAUDE_PIECES["res-auth"]),
        "codex-fail": jl(CODEX_PIECES["failed-noreset"]),
        "codex-info": jl(CODEX_PIECES["error-info"]),
        "codex-clock": jl(CODEX_PIECES["failed-clock"]),
        "codex-plain": jl(CODEX_PIECES["failed-plain"]),
        "codex-ok": jl(CODEX_PIECES["thread"], CODEX_PIECES["usage"]),
    }
    for fname, evs in claude_files.items():
        for sname in ("empty", "claude-fail", "claude-ok", "claude-auth"):
            for code, started in itertools.product((0, 1, None), (STARTED, None)):
                yield (f"sess-cl:{fname}:{sname}:{code}:{'s' if started else 'n'}", "claude",
                       (stdouts[sname], ""), code, UTC, NOW, ("claude", evs, started))
    for fname, evs in codex_files.items():
        for sname in ("empty", "codex-fail", "codex-info", "codex-clock", "codex-plain", "codex-ok"):
            for code, started in itertools.product((0, 1, None), (STARTED, None)):
                yield (f"sess-cx:{fname}:{sname}:{code}:{'s' if started else 'n'}", "codex",
                       (stdouts[sname], ""), code, TEH, NOW, ("codex", evs, started))


def all_cases():
    yield from _fixture_cases()
    yield from _piece_cases()
    yield from _session_cases()


def _write_session(root: Path, kind: str, evs: list, started) -> Path:
    if kind == "claude":
        d = root / "projects" / "-home-x-repo"
        d.mkdir(parents=True)
        f = d / "abc.jsonl"
    else:
        d = root / "sessions" / "2026" / "10" / "24"
        d.mkdir(parents=True)
        f = d / "rollout-x.jsonl"
    f.write_text(jl(*evs), encoding="utf-8")
    stamp = (NOW - timedelta(minutes=10)).timestamp()
    os.utime(f, (stamp, stamp))
    return root


def run_case(provider, outs, code, tz, now, sess):
    stdout, stderr = outs
    fn = classify_claude if provider == "claude" else classify_codex
    if sess is None:
        return fn(stdout, stderr, code, now, tz)
    kind, evs, started = sess
    with tempfile.TemporaryDirectory() as d:
        home = _write_session(Path(d), kind, evs, started)
        return fn(stdout, stderr, code, now, tz, home, started)


def summarise(oc) -> str:
    reset = oc.reset_at.astimezone(UTC).isoformat() if oc.reset_at else "-"
    return "|".join([
        oc.kind, oc.window, oc.model, reset, "-" if oc.retry_after_s is None else repr(oc.retry_after_s),
        oc.source, oc.detail, str(oc.session_id), json.dumps(oc.usage, sort_keys=True)])


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:10]


def snapshot(readable: dict[str, str] | None = None) -> dict[str, str]:
    """case-id hash -> hash of the full answer (the file stays small; `readable` collects id -> answer)."""
    snap: dict[str, str] = {}
    for cid, provider, outs, code, tz, now, sess in all_cases():
        assert digest(cid) not in snap, cid
        try:
            answer = summarise(run_case(provider, outs, code, tz, now, sess))
        except Exception as e:      # an exception is behaviour too
            answer = f"EXC|{type(e).__name__}|{e}"
        snap[digest(cid)] = digest(answer)
        if readable is not None:
            readable[cid] = answer
    return snap


def parse_when_snapshot() -> dict[str, str]:
    texts = ["1:52 PM", "1:52 AM", "10:10pm", "11am", "23:30", "Oct 9th, 2026 11:19 PM", "20 Aug 2026, 16:29",
             "Aug 10, 2026, 5:59 AM", "Mon 12:00am", "Sunday 9am", "Friday 11:30 PM", "Oct 27", "27 Oct 9am",
             "Oct 27 9:05 AM", "Feb 29 10am", "Dec 31 11pm", "2026-10-30", "2026-10-30T12:00:00Z",
             "2026-10-30T12:00:00", "in 2 hours", "3h 20m", "in 1 days 2 hours 5 minutes", "at 3am", "on Oct 25 3am",
             "whenever", "", "   ", "Xyz 10am", "13:61", "Mar 8 2:30 AM", "Nov 1 1:30 AM", "midnight"]
    out: dict[str, str] = {}
    nows = [datetime(2026, 10, 24, 12, 0, tzinfo=TEH), datetime(2026, 10, 24, 23, 59, tzinfo=TEH),
            datetime(2026, 12, 31, 23, 0, tzinfo=UTC), datetime(2026, 3, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 25, 0, 30, tzinfo=BER)]
    for t, now, tz in itertools.product(texts, nows, (UTC, TEH, BER, ZoneInfo("America/New_York"))):
        r = parse_when(t, now, tz)
        out[digest(f"{t}|{now.isoformat()}|{getattr(tz, 'key', 'utc')}")] = digest(
            r.astimezone(UTC).isoformat() if r else "-")
    return out


def full_snapshot(readable: dict[str, str] | None = None) -> dict:
    return {"classify": snapshot(readable), "parse_when": parse_when_snapshot()}


class Characterization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cls.readable: dict[str, str] = {}
        cls.current = full_snapshot(cls.readable)
        cls.names = {digest(k): k for k in cls.readable}

    def test_the_corpus_is_large_enough_to_mean_something(self):
        self.assertGreater(len(self.recorded["classify"]), 2000)
        self.assertGreater(len(self.recorded["parse_when"]), 500)

    def test_every_case_has_a_recorded_answer(self):
        self.assertEqual(sorted(self.current["classify"]), sorted(self.recorded["classify"]))
        self.assertEqual(sorted(self.current["parse_when"]), sorted(self.recorded["parse_when"]))

    def test_classify_outputs_are_unchanged(self):
        changed = [self.names[k] for k, v in self.current["classify"].items()
                   if self.recorded["classify"].get(k) != v]
        self.assertEqual(changed[:5], [], f"{len(changed)} of {len(self.current['classify'])} cases changed")

    def test_parse_when_outputs_are_unchanged(self):
        changed = [k for k, v in self.current["parse_when"].items() if self.recorded["parse_when"].get(k) != v]
        self.assertEqual(changed[:5], [], f"{len(changed)} cases changed")

    def test_a_good_share_of_cases_reach_each_kind(self):
        kinds: dict[str, int] = {}
        for v in self.readable.values():
            k = v.split("|", 1)[0]
            kinds[k] = kinds.get(k, 0) + 1
        for kind in ("limit", "auth", "transient", "success", "unknown", "sandbox"):
            self.assertGreater(kinds.get(kind, 0), 20, kinds)


if __name__ == "__main__":
    if "--write" in sys.argv:
        data = full_snapshot()
        SNAPSHOT.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        print({k: len(v) for k, v in data.items()}, "->", SNAPSHOT)
    else:
        unittest.main(argv=[sys.argv[0]])
