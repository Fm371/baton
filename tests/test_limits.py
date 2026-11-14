import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from baton.limits import (classify_claude, classify_codex, parse_duration, parse_when,
                          unmatched_failure_warning, UNMATCHED_NOTE)

FIX = Path(__file__).parent / "fixtures" / "limits"

TEH = ZoneInfo("Asia/Tehran")
UTC = timezone.utc


def at(y, mo, d, h, mi, tz=TEH):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


class ParseWhen(unittest.TestCase):
    def test_bare_time_later_today(self):
        now = at(2026, 10, 24, 12, 0)
        self.assertEqual(parse_when("1:52 PM", now, TEH), at(2026, 10, 24, 13, 52))

    def test_bare_time_after_midnight_is_tomorrow(self):
        now = at(2026, 10, 24, 23, 50)
        self.assertEqual(parse_when("1:52 AM", now, TEH), at(2026, 10, 25, 1, 52))

    def test_time_a_moment_ago_is_now_not_tomorrow(self):
        now = at(2026, 10, 24, 13, 52) + timedelta(seconds=40)
        self.assertEqual(parse_when("1:52 PM", now, TEH), at(2026, 10, 24, 13, 52))

    def test_claude_style_lowercase(self):
        now = at(2026, 10, 24, 18, 0)
        self.assertEqual(parse_when("10:10pm", now, TEH), at(2026, 10, 24, 22, 10))
        self.assertEqual(parse_when("11am", now, UTC), datetime(2026, 10, 25, 11, 0, tzinfo=UTC))

    def test_full_dates(self):
        now = at(2026, 10, 1, 9, 0)
        self.assertEqual(parse_when("Oct 9th, 2026 11:19 PM", now, TEH), at(2026, 10, 9, 23, 19))
        self.assertEqual(parse_when("20 Aug 2026, 16:29", now, TEH), at(2026, 8, 20, 16, 29))
        self.assertEqual(parse_when("Aug 10, 2026, 5:59 AM", now, TEH), at(2026, 8, 10, 5, 59))

    def test_weekday(self):
        now = at(2026, 10, 24, 9, 0)            # a Saturday
        self.assertEqual(parse_when("Mon 12:00am", now, TEH), at(2026, 10, 26, 0, 0))

    def test_relative(self):
        now = at(2026, 10, 24, 9, 0)
        self.assertEqual(parse_when("in 1 days 2 hours 5 minutes", now, TEH),
                         now + timedelta(days=1, hours=2, minutes=5))
        self.assertEqual(parse_duration("3h 20m"), timedelta(hours=3, minutes=20))
        self.assertIsNone(parse_duration("10:10pm"))

    def test_garbage(self):
        self.assertIsNone(parse_when("whenever", at(2026, 10, 24, 9, 0), TEH))


def jl(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class Claude(unittest.TestCase):
    now = at(2026, 10, 24, 18, 0)

    def test_session_limit_result_text(self):
        out = jl({"type": "system", "subtype": "init", "session_id": "s-1"},
                 {"type": "result", "subtype": "success", "is_error": True, "session_id": "s-1",
                  "result": "You've hit your session limit · resets 10:10pm (Asia/Tehran)"})
        oc = classify_claude(out, "", 1, self.now, UTC)
        self.assertEqual(oc.kind, "limit")
        self.assertEqual(oc.reset_at, at(2026, 10, 24, 22, 10))
        self.assertEqual(oc.window, "five_hour")
        self.assertEqual(oc.session_id, "s-1")

    def test_weekly_limit(self):
        out = jl({"type": "result", "is_error": True,
                  "result": "You’ve hit your weekly limit ∙ resets Mon 12:00am (UTC)"})
        oc = classify_claude(out, "", 1, self.now, TEH)
        self.assertEqual(oc.kind, "limit")
        self.assertEqual(oc.window, "weekly")
        self.assertEqual(oc.reset_at, datetime(2026, 10, 26, 0, 0, tzinfo=UTC))

    def test_rate_limit_event(self):
        reset = int(at(2026, 10, 24, 21, 0).timestamp())
        out = jl({"type": "rate_limit_event", "rate_limit_info": {
                     "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour"}},
                 {"type": "result", "is_error": True, "result": "API Error"})
        oc = classify_claude(out, "", 1, self.now, TEH)
        self.assertEqual((oc.kind, oc.window, oc.reset_at), ("limit", "five_hour", at(2026, 10, 24, 21, 0)))
        self.assertTrue(oc.source.startswith("claude:rate_limit_event"))

    def test_allowed_event_is_not_a_limit(self):
        out = jl({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1793000000}},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "done"})
        self.assertEqual(classify_claude(out, "", 0, self.now, TEH).kind, "success")

    def test_agent_talking_about_limits_is_not_a_limit(self):
        out = jl({"type": "assistant", "message": {"content": [
                     {"type": "text", "text": "I added a usage limit reached check; you've hit your limit message"}]}},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "done",
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 900}, "num_turns": 4})
        oc = classify_claude(out, "", 0, self.now, TEH)
        self.assertEqual(oc.kind, "success")
        self.assertEqual(oc.usage["cache_read_input_tokens"], 900)
        self.assertEqual(oc.usage["num_turns"], 4)

    def test_retried_429_that_recovered_is_success(self):
        out = jl({"type": "system", "subtype": "api_retry", "error_status": 429, "error": "rate_limit"},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
        self.assertEqual(classify_claude(out, "", 0, self.now, TEH).kind, "success")

    def test_silent_weekly_exit_found_in_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d) / "projects" / "-home-x-repo"
            proj.mkdir(parents=True)
            (proj / "abc.jsonl").write_text(jl(
                {"type": "user", "message": {"content": "hi"}},
                {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                 "message": {"content": [{"type": "text",
                                          "text": "You've hit your weekly limit · resets Oct 27, 3pm (Asia/Tehran)"}]}}))
            oc = classify_claude("", "", 1, self.now, TEH, claude_home=Path(d),
                                 started=datetime.now(UTC) - timedelta(seconds=30))
            self.assertEqual(oc.kind, "limit")
            self.assertEqual(oc.window, "weekly")
            self.assertEqual(oc.reset_at, at(2026, 10, 27, 15, 0))
            self.assertIn("transcript", oc.source)

    def test_unknown_exit(self):
        self.assertEqual(classify_claude("", "segfault", 139, self.now, TEH).kind, "unknown")

    def test_auth(self):
        out = jl({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"})
        self.assertEqual(classify_claude(out, "", 1, self.now, TEH).kind, "auth")

    def test_wrap_up_without_banner(self):
        reset = int(at(2026, 10, 24, 20, 30).timestamp())
        out = jl({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": reset}},
                 {"type": "result", "subtype": "success", "is_error": False,
                  "result": "Approaching your 5-hour usage limit — Claude will wrap up the current step"})
        oc = classify_claude(out, "", 0, self.now, TEH)
        self.assertEqual((oc.kind, oc.reset_at), ("limit", at(2026, 10, 24, 20, 30)))


class Codex(unittest.TestCase):
    now = at(2026, 10, 24, 12, 0)
    MSG = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
           "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 1:52 PM.")

    def test_usage_limit_jsonl(self):
        out = jl({"type": "thread.started", "thread_id": "th-9"}, {"type": "turn.started"},
                 {"type": "error", "message": self.MSG},
                 {"type": "turn.failed", "error": {"message": self.MSG}})
        oc = classify_codex(out, "", 1, self.now, TEH)
        self.assertEqual(oc.kind, "limit")
        self.assertEqual(oc.reset_at, at(2026, 10, 24, 13, 52))
        self.assertEqual(oc.window, "five_hour")
        self.assertEqual(oc.session_id, "th-9")

    def test_date_format_is_weekly(self):
        msg = "You've hit your usage limit. try again at Oct 27th, 2026 9:05 AM."
        oc = classify_codex(jl({"type": "error", "message": msg}), "", 1, self.now, TEH)
        self.assertEqual((oc.kind, oc.window, oc.reset_at), ("limit", "weekly", at(2026, 10, 27, 9, 5)))

    def test_relative(self):
        msg = "You've hit your usage limit. Try again in 2 hours 5 minutes."
        oc = classify_codex(jl({"type": "error", "message": msg}), "", 1, self.now, TEH)
        self.assertEqual(oc.reset_at, self.now + timedelta(hours=2, minutes=5))

    def test_429_is_transient(self):
        msg = ("http 429 Too Many Requests: You've exceeded the rate limit, please slow down and "
               "try again after 60.616292 seconds.")
        oc = classify_codex("", msg, 1, self.now, TEH)
        self.assertEqual(oc.kind, "transient")
        self.assertAlmostEqual(oc.retry_after_s, 60.616292)

    def test_auth(self):
        oc = classify_codex(jl({"type": "error", "message": "Not logged in. Please run codex login"}), "", 1,
                            self.now, TEH)
        self.assertEqual(oc.kind, "auth")

    def test_rollout_reset_when_message_has_none(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "sessions" / "2026" / "10" / "24"
            sess.mkdir(parents=True)
            reset = int(at(2026, 10, 24, 16, 0).timestamp())
            (sess / "rollout-x.jsonl").write_text(jl(
                {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 100.0, "resets_at": reset},
                    "secondary": {"used_percent": 40.0, "resets_at": reset + 400000}}}},
                {"type": "event_msg", "payload": {"type": "task_complete", "error": {
                    "codex_error_info": "usage_limit_exceeded"}}}))
            out = jl({"type": "turn.failed", "error": {"message": "usage_limit_exceeded"}})
            oc = classify_codex(out, "", 1, self.now, TEH, codex_home=Path(d),
                                started=datetime.now(UTC) - timedelta(seconds=10))
            self.assertEqual(oc.kind, "limit")
            self.assertEqual(oc.reset_at, at(2026, 10, 24, 16, 0))

    def test_success_and_usage(self):
        out = jl({"type": "thread.started", "thread_id": "t"},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "hit your usage limit? no"}},
                 {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                                      "output_tokens": 5}},
                 {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40,
                                                      "output_tokens": 5}})
        oc = classify_codex(out, "", 0, self.now, TEH)
        self.assertEqual(oc.kind, "success")
        self.assertEqual(oc.usage, {"input_tokens": 150, "cached_input_tokens": 120, "output_tokens": 10})


# (fixture, provider, exit code, kind, window, reset_at, retry_after_s); see fixtures/limits/README.md
FIXTURES = [
    ("claude-session-limit", "claude", 1, "limit", "five_hour", at(2026, 10, 24, 22, 10), None),
    ("claude-weekly-limit", "claude", 1, "limit", "weekly", datetime(2026, 10, 26, 0, 0, tzinfo=UTC), None),
    ("claude-wrap-up", "claude", 0, "limit", "five_hour", datetime.fromtimestamp(1792837800, UTC), None),
    ("claude-auth", "claude", 1, "auth", "", None, None),
    ("claude-agent-talks-about-limits", "claude", 0, "success", "", None, None),
    ("codex-usage-limit-clock", "codex", 1, "limit", "five_hour", at(2026, 10, 24, 13, 52), None),
    ("codex-usage-limit-date", "codex", 1, "limit", "weekly", at(2026, 10, 27, 9, 5), None),
    ("codex-usage-limit-relative", "codex", 1, "limit", "five_hour", at(2026, 10, 24, 14, 5), None),
    ("codex-auth", "codex", 1, "auth", "", None, None),
    ("claude-out-of-credits", "claude", 1, "auth", "", None, None),
    ("codex-transient-429", "codex", 1, "transient", "", None, 60.616292),
    ("claude-code-usage-limit-no-wording", "claude", 1, "limit", "unknown", None, None),
    ("claude-clean-exit-stderr-talk", "claude", 0, "success", "", None, None),
    ("codex-code-insufficient-quota", "codex", 1, "auth", "", None, None),
    ("codex-status-429-no-wording", "codex", 1, "transient", "", None, None),
    ("codex-clean-exit-talks-about-limits", "codex", 0, "success", "", None, None),
]


class Fixtures(unittest.TestCase):
    now = at(2026, 10, 24, 12, 0)

    def test_every_fixture_file_is_in_the_table(self):
        listed = {f[0] for f in FIXTURES}
        on_disk = {p.name.removesuffix(".stderr.txt").removesuffix(".txt")
                   for p in FIX.glob("*.txt")}
        self.assertEqual(listed, on_disk)

    def test_fixtures(self):
        for name, prov, code, kind, window, reset, retry in FIXTURES:
            with self.subTest(name):
                err = FIX / f"{name}.stderr.txt"
                stdout, stderr = ("", err.read_text()) if err.exists() else ((FIX / f"{name}.txt").read_text(), "")
                fn = classify_claude if prov == "claude" else classify_codex
                now = at(2026, 10, 24, 18, 0) if prov == "claude" else self.now
                oc = fn(stdout, stderr, code, now, TEH)
                self.assertEqual(oc.kind, kind)
                self.assertEqual(oc.window, window)
                self.assertEqual(oc.reset_at, reset)
                if retry is not None:
                    self.assertAlmostEqual(oc.retry_after_s, retry)
                self.assertIsNone(unmatched_failure_warning(prov, oc, stdout, stderr, code))


class StructuredFirst(unittest.TestCase):
    now = at(2026, 10, 24, 12, 0)

    def test_code_beats_missing_wording_and_keeps_text_reset(self):
        out = jl({"type": "error", "message": "usage_limit_exceeded: try again at 1:52 PM.",
                  "codex_error_info": {"usage_limit_exceeded": {}}})
        oc = classify_codex(out, "", 1, self.now, TEH)
        self.assertEqual((oc.kind, oc.reset_at), ("limit", at(2026, 10, 24, 13, 52)))

    def test_plain_stdout_and_stderr_ignored_on_clean_exit(self):
        for fn in (classify_claude, classify_codex):
            oc = fn("You have hit your limit. Not logged in. 401", "usage limit reached", 0, self.now, TEH)
            self.assertEqual(oc.kind, "success", fn.__name__)

    def test_plain_stdout_and_stderr_still_count_on_failure(self):
        oc = classify_claude("", "You've hit your session limit · resets 10:10pm (Asia/Tehran)", 1, self.now, TEH)
        self.assertEqual(oc.kind, "limit")

    def test_status_code_401_is_auth(self):
        oc = classify_codex(jl({"type": "error", "message": "denied", "status": 401}), "", 1, self.now, TEH)
        self.assertEqual(oc.kind, "auth")

    def test_transient_code_on_clean_exit_is_recovered(self):
        out = jl({"type": "system", "subtype": "api_retry", "status": 429},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
        self.assertEqual(classify_claude(out, "", 0, self.now, TEH).kind, "success")

    def test_text_in_assistant_message_never_decides(self):
        out = jl({"type": "assistant", "message": {"content": [{"type": "text",
                  "text": "usage_limit_exceeded, insufficient_quota, status 429"}]}},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
        self.assertEqual(classify_claude(out, "", 0, self.now, TEH).kind, "success")


class UnmatchedFailure(unittest.TestCase):
    now = at(2026, 10, 24, 12, 0)

    def test_unknown_failure_warns_with_excerpt(self):
        oc = classify_claude("", "Quota exhausted for today, back tomorrow", 1, self.now, TEH)
        self.assertEqual(oc.kind, "unknown")
        w = unmatched_failure_warning("claude", oc, "", "Quota exhausted for today, back tomorrow", 1)
        self.assertIn(UNMATCHED_NOTE, w)
        self.assertIn("Quota exhausted for today", w)
        self.assertIn("exit 1", w)

    def test_long_output_is_shortened(self):
        oc = classify_codex("", "x" * 5000, 2, self.now, TEH)
        self.assertEqual(oc.kind, "unknown")
        self.assertLess(len(unmatched_failure_warning("codex", oc, "", "x" * 5000, 2)), 400)

    def test_no_output(self):
        oc = classify_claude("", "", 139, self.now, TEH)
        self.assertIn("(no output)", unmatched_failure_warning("claude", oc, "", "", 139))

    def test_matched_or_successful_runs_do_not_warn(self):
        oc = classify_claude("", "", 0, self.now, TEH)
        self.assertIsNone(unmatched_failure_warning("claude", oc, "", "", 0))

    def test_bad_zone_name_falls_back_instead_of_raising(self):
        out = jl({"type": "result", "is_error": True,
                  "result": "You've hit your session limit · resets 10:10pm (Not/AZone)"})
        oc = classify_claude(out, "", 1, self.now, TEH)
        self.assertEqual((oc.kind, oc.reset_at), ("limit", at(2026, 10, 24, 22, 10)))


if __name__ == "__main__":
    unittest.main()


class CodexCreditsAreALimit(unittest.TestCase):
    NOW = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)

    def _out(self, extra_events=()):
        evs = [{"type": "error", "message": "Your workspace is out of credits. Ask your owner to refill."},
               {"type": "turn.failed", "error": {"message": "Your workspace is out of credits."}},
               *extra_events]
        return classify_codex("\n".join(json.dumps(e) for e in evs), "", 1, self.NOW, UTC)

    def test_credits_wait_for_the_reset_in_codexs_rate_limit_event(self):
        reset = int((self.NOW + timedelta(minutes=5)).timestamp())
        o = self._out([{"type": "rate_limits", "rate_limits": {"primary": {"used_percent": 100, "resets_at": reset},
                        "rate_limit_reached_type": "workspace_member_credits_depleted"}}])
        self.assertEqual(o.kind, "limit")
        self.assertEqual(o.reset_at, self.NOW + timedelta(minutes=5))

    def test_credits_without_a_reset_retry_hourly(self):
        o = self._out()
        self.assertEqual(o.kind, "limit")
        self.assertEqual(o.reset_at, self.NOW + timedelta(hours=1))
