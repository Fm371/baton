"""Lanes: the pure parts. choose_provider() and select() change nothing and use no threads."""

import unittest
from datetime import datetime, timedelta, timezone

from baton.scheduler import Candidate, ProviderStatus, Scheduler, choose_provider, select

T0 = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
PROVIDERS = ["claude", "codex"]
SLOTS = {"claude": 1, "codex": 1}


def status(**kw):
    """status(claude=T0 + H): claude limited until then; status(codex="auth"): codex disabled."""
    out = {p: ProviderStatus() for p in PROVIDERS}
    for p, v in kw.items():
        if isinstance(v, str):
            out[p].disabled = v
        else:
            out[p].limited_until, out[p].window = v, "five_hour"
    return out


def cand(tid, lane=None, repo=None, allowed=None, last=None):
    return Candidate(tid, allowed, lane or tid, repo or tid, last)


def pick_ids(candidates, st=None, busy_lanes=(), busy_repos=(), busy_slots=None, max_new=2):
    got = select(candidates, PROVIDERS, st or status(), SLOTS, set(busy_lanes), set(busy_repos),
                 busy_slots or {}, T0, max_new)
    return [(c.id, prov) for c, prov in got]


class ChooseProviderTest(unittest.TestCase):
    def choose(self, st=None, allowed=None, last=None, busy=None):
        return choose_provider(PROVIDERS, st or status(), allowed, last, T0, busy or {}, SLOTS)

    def test_the_first_available_provider(self):
        self.assertEqual(self.choose().provider, "claude")

    def test_a_task_stays_on_the_provider_it_ran_on(self):
        self.assertEqual(self.choose(last="codex").provider, "codex")

    def test_a_limited_last_provider_moves_the_task(self):
        self.assertEqual(self.choose(status(codex=T0 + H), last="codex").provider, "claude")

    def test_a_busy_slot_waits_and_never_moves_the_task(self):
        p = self.choose(busy={"claude": 1})
        self.assertIsNone(p.provider)
        self.assertEqual(p.slot_busy, "claude")

    def test_a_limit_does_move_it(self):
        self.assertEqual(self.choose(status(claude=T0 + H), busy={"claude": 1}).provider, "codex")

    def test_the_allowed_list_is_respected(self):
        self.assertEqual(self.choose(allowed=["codex"]).provider, "codex")
        p = self.choose(status(codex=T0 + 2 * H), allowed=["codex"])
        self.assertEqual((p.provider, p.wait_until), ("codex", T0 + 2 * H))

    def test_all_limited_waits_for_the_earliest_reset(self):
        p = self.choose(status(claude=T0 + 3 * H, codex=T0 + H))
        self.assertEqual((p.provider, p.wait_until), ("codex", T0 + H))

    def test_a_reset_in_the_past_counts_as_available(self):
        self.assertEqual(self.choose(status(claude=T0 - H)).provider, "claude")

    def test_nothing_usable(self):
        p = self.choose(status(claude="auth", codex="sandbox"))
        self.assertEqual((p.provider, p.wait_until), (None, None))
        self.assertIn("no usable provider", p.reason)
        self.assertIn("none of", self.choose(allowed=["nobody"]).reason)

    def test_scheduler_pick_for_does_not_move_current(self):
        s = Scheduler(PROVIDERS, "claude")
        s.mark_limited("claude", T0 + H, "five_hour")
        self.assertEqual(s.pick_for(None, None, T0, {}, SLOTS).provider, "codex")
        self.assertEqual(s.current, "claude")


class SelectTest(unittest.TestCase):
    def test_ready_order_and_the_lane_limit(self):
        got = pick_ids([cand("a", allowed=("claude",)), cand("b", allowed=("codex",)),
                        cand("c", allowed=("claude",))], max_new=2)
        self.assertEqual(got, [("a", "claude"), ("b", "codex")])
        self.assertEqual(pick_ids([cand("a")], max_new=0), [])

    def test_a_busy_lane_or_repository_is_skipped(self):
        c = [cand("a", lane="x"), cand("b", lane="y", repo="r2")]
        self.assertEqual(pick_ids(c, busy_lanes={"x"}), [("b", "claude")])
        self.assertEqual(pick_ids(c, busy_repos={"r2"}), [("a", "claude")])

    def test_two_tasks_in_one_lane_never_start_together(self):
        self.assertEqual(pick_ids([cand("a", lane="db", repo="r1"), cand("b", lane="db", repo="r2")]),
                         [("a", "claude")])

    def test_two_tasks_in_one_repository_never_start_together(self):
        self.assertEqual(pick_ids([cand("a", lane="x", repo="r"), cand("b", lane="y", repo="r")]),
                         [("a", "claude")])

    def test_a_busy_slot_holds_the_task_but_not_the_next_one(self):
        c = [cand("a", allowed=("claude",)), cand("b", allowed=("claude",)), cand("c", allowed=("codex",))]
        self.assertEqual(pick_ids(c, max_new=3), [("a", "claude"), ("c", "codex")])
        self.assertEqual(pick_ids(c, busy_slots={"claude": 1}, max_new=3), [("c", "codex")])

    def test_without_an_allowed_list_a_busy_slot_does_not_send_it_to_the_other_provider(self):
        self.assertEqual(pick_ids([cand("a"), cand("b")]), [("a", "claude")])

    def test_slots_above_one(self):
        got = select([cand("a"), cand("b")], PROVIDERS, status(), {"claude": 2, "codex": 1},
                     set(), set(), {}, T0, 2)
        self.assertEqual([(c.id, p) for c, p in got], [("a", "claude"), ("b", "claude")])

    def test_a_limited_provider_lets_the_next_task_go_ahead(self):
        c = [cand("a", allowed=("claude",)), cand("b", allowed=("codex",))]
        self.assertEqual(pick_ids(c, status(claude=T0 + H)), [("b", "codex")])

    def test_sticky_last_provider(self):
        self.assertEqual(pick_ids([cand("a", last="codex")]), [("a", "codex")])
        self.assertEqual(pick_ids([cand("a", last="codex")], status(codex=T0 + H)), [("a", "claude")])

    def test_a_task_that_needs_no_provider_takes_a_lane_but_no_slot(self):
        c = [cand("a", allowed=("claude",)), cand("check", allowed=())]
        self.assertEqual(pick_ids(c, busy_slots={"claude": 1}), [("check", None)])

    def test_it_changes_nothing(self):
        st, busy = status(claude=T0 + H), {"codex": 0}
        pick_ids([cand("a")], st, busy_slots=busy)
        self.assertEqual((st["claude"].limited_until, busy), (T0 + H, {"codex": 0}))


if __name__ == "__main__":
    unittest.main()
