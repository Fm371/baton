import unittest
from datetime import datetime, timedelta, timezone

from baton.scheduler import Scheduler

T0 = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.s = Scheduler(["claude", "codex"], "claude")

    def test_allowed_providers_never_fall_back(self):
        self.s.mark_limited("claude", T0 + 2 * H, "five_hour")
        p = self.s.pick(T0, ["claude"])
        self.assertEqual((p.provider, p.wait_until), ("claude", T0 + 2 * H))
        self.s.disable("claude", "auth")
        p = self.s.pick(T0, ["claude"])
        self.assertIsNone(p.provider)
        self.assertEqual(self.s.pick(T0).provider, "codex")

    def test_starts_on_current(self):
        p = self.s.pick(T0)
        self.assertEqual((p.provider, p.wait_until, p.switched_from), ("claude", None, None))

    def test_switches_when_limited(self):
        self.s.mark_limited("claude", T0 + 3 * H, "five_hour")
        p = self.s.pick(T0)
        self.assertEqual((p.provider, p.switched_from), ("codex", "claude"))

    def test_sticky_after_the_other_recovers(self):
        self.s.mark_limited("claude", T0 + H, "five_hour")
        self.s.pick(T0)                                 # -> codex
        p = self.s.pick(T0 + 2 * H)                     # claude is back, codex still fine
        self.assertEqual((p.provider, p.switched_from), ("codex", None))

    def test_both_limited_waits_for_earliest(self):
        self.s.mark_limited("claude", T0 + 4 * H, "five_hour")
        self.s.pick(T0)                                 # -> codex
        self.s.mark_limited("codex", T0 + 2 * H, "five_hour")
        p = self.s.pick(T0 + 0.5 * H)
        self.assertEqual((p.provider, p.wait_until), ("codex", T0 + 2 * H))
        p = self.s.pick(T0 + 2 * H)                     # after the sleep
        self.assertEqual((p.provider, p.wait_until), ("codex", None))

    def test_earliest_reset_can_be_the_other_provider(self):
        self.s.mark_limited("claude", T0 + 1 * H, "five_hour")
        self.s.pick(T0)                                 # -> codex
        self.s.mark_limited("codex", T0 + 5 * H, "five_hour")
        p = self.s.pick(T0 + 0.2 * H)
        self.assertEqual((p.provider, p.wait_until), ("claude", T0 + H))
        p = self.s.pick(T0 + H)
        self.assertEqual((p.provider, p.switched_from), ("claude", "codex"))

    def test_weekly_limit_loses_to_five_hour(self):
        self.s.mark_limited("claude", T0 + 72 * H, "weekly")
        self.s.mark_limited("codex", T0 + 3 * H, "five_hour")
        self.assertEqual(self.s.pick(T0).provider, "codex")

    def test_disabled(self):
        self.s.disable("claude", "auth")
        self.assertEqual(self.s.pick(T0).provider, "codex")
        self.s.disable("codex", "auth")
        p = self.s.pick(T0)
        self.assertIsNone(p.provider)
        self.assertIn("no usable provider", p.reason)

    def test_select_skips_tasks_whose_providers_are_limited(self):
        self.s.mark_limited("claude", T0 + H, "five_hour")
        ready = [("a", ["claude"]), ("b", None), ("c", ["codex"])]
        self.assertEqual(self.s.select(ready, T0), "b")            # first runnable, in queue order
        self.assertEqual(self.s.select(ready[:1], T0), None)       # only waiting tasks
        self.assertEqual(self.s.select(ready, T0 + H), "a")        # claude is back
        self.assertEqual(self.s.current, "claude")                 # select changes nothing
        self.assertEqual(self.s.status["claude"].limited_until, T0 + H)

    def test_select_ignores_disabled_providers(self):
        self.s.disable("codex", "auth")
        self.assertEqual(self.s.select([("a", ["codex"]), ("b", ["claude"])], T0), "b")

    def test_round_trip(self):
        from baton.scheduler import ProviderStatus
        self.s.mark_limited("claude", T0 + H, "weekly")
        d = self.s.to_dict()
        s2 = Scheduler(["claude", "codex"], d["current"],
                       {p: ProviderStatus.from_dict(x) for p, x in d["providers"].items()})
        self.assertEqual(s2.status["claude"].limited_until, T0 + H)
        self.assertEqual(s2.status["claude"].window, "weekly")


if __name__ == "__main__":
    unittest.main()
