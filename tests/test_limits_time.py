"""Reset times around clock changes, the old epoch format, and the machine's own zone."""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from baton.clock import local_zone
from baton.limits import classify, parse_when, resolve_local

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


class ResolveLocalTest(unittest.TestCase):
    def test_an_ordinary_time(self):
        d = resolve_local(date(2026, 6, 1), 13, 52, NY)
        self.assertEqual(d.astimezone(UTC), datetime(2026, 6, 1, 17, 52, tzinfo=UTC))

    def test_a_time_inside_the_spring_gap_moves_to_after_the_gap(self):
        # 2026-03-08 02:30 does not exist in New York; the clock jumps from 02:00 to 03:00
        d = resolve_local(date(2026, 3, 8), 2, 30, NY)
        self.assertEqual(d.astimezone(UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC))      # 03:30 EDT
        self.assertEqual((d.hour, d.minute), (3, 30))

    def test_an_ambiguous_time_takes_the_later_one(self):
        # 2026-11-01 01:30 happens twice in New York (EDT, then EST)
        d = resolve_local(date(2026, 11, 1), 1, 30, NY)
        self.assertEqual(d.astimezone(UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC))      # the EST one

    def test_berlin_fall_back(self):
        d = resolve_local(date(2026, 10, 25), 2, 30, BERLIN)
        self.assertEqual(d.astimezone(UTC), datetime(2026, 10, 25, 1, 30, tzinfo=UTC))     # CET, the later


class ParseWhenAcrossClockChangesTest(unittest.TestCase):
    def test_a_reset_in_the_spring_gap(self):
        now = datetime(2026, 3, 8, 5, 30, tzinfo=UTC)            # 00:30 EST
        got = parse_when("2:30 AM", now, NY)
        self.assertEqual(got.astimezone(UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC))

    def test_a_reset_in_the_repeated_hour(self):
        now = datetime(2026, 11, 1, 4, 10, tzinfo=UTC)           # 00:10 EDT
        got = parse_when("1:30 AM", now, NY)
        self.assertEqual(got.astimezone(UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC))

    def test_tomorrow_across_the_fall_back_keeps_the_wall_clock(self):
        now = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)          # 11:00 EDT on Oct 31, 9 AM has passed
        got = parse_when("9:00 AM", now, NY)
        self.assertEqual(got.astimezone(NY).replace(tzinfo=None), datetime(2026, 11, 1, 9, 0))
        self.assertEqual(got.astimezone(UTC), datetime(2026, 11, 1, 14, 0, tzinfo=UTC))   # EST after the change

    def test_a_weekday_across_the_spring_forward(self):
        now = datetime(2026, 3, 6, 15, 0, tzinfo=UTC)            # Friday 10:00 EST
        got = parse_when("Mon 9:00 AM", now, NY)
        self.assertEqual(got.astimezone(NY).replace(tzinfo=None), datetime(2026, 3, 9, 9, 0))
        self.assertEqual(got.astimezone(UTC), datetime(2026, 3, 9, 13, 0, tzinfo=UTC))    # EDT

    def test_a_dated_reset_in_a_named_zone(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        got = parse_when("Nov 3, 9am", now, BERLIN)
        self.assertEqual(got.astimezone(UTC), datetime(2026, 11, 3, 8, 0, tzinfo=UTC))    # CET


class LegacyEpochTest(unittest.TestCase):
    def test_the_old_claude_format_yields_its_reset(self):
        reset = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        now = reset - timedelta(hours=2)
        out = f"Claude AI usage limit reached|{int(reset.timestamp())}"
        oc = classify("claude", out, "", 1, now, UTC)
        self.assertEqual(oc.kind, "limit")
        self.assertEqual(oc.reset_at, reset)
        self.assertEqual(oc.window, "five_hour")

    def test_without_an_epoch_nothing_is_invented(self):
        oc = classify("claude", "Claude AI usage limit reached", "", 1, datetime(2026, 9, 21, tzinfo=UTC), UTC)
        self.assertEqual(oc.kind, "limit")
        self.assertIsNone(oc.reset_at)


class LocalZoneTest(unittest.TestCase):
    def test_tz_variable_names_a_real_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"}):
            z = local_zone()
        self.assertEqual(getattr(z, "key", None), "Europe/Berlin")

    def test_a_colon_prefixed_name_works(self):
        with mock.patch.dict(os.environ, {"TZ": ":Asia/Tehran"}):
            self.assertEqual(getattr(local_zone(), "key", None), "Asia/Tehran")

    def test_always_returns_a_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}):
            self.assertIsNotNone(local_zone().utcoffset(datetime(2026, 1, 1)))


if __name__ == "__main__":
    unittest.main()
