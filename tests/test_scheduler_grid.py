"""Scheduler.pick and choose_provider over a grid of states (pins every answer so the functions can
be restructured). Generated from the code before the refactor; the tables are zlib+base64 of repr(rows)."""

import ast
import base64
import itertools
import unittest
import zlib
from datetime import datetime, timedelta, timezone

from baton.scheduler import ProviderStatus, Scheduler, choose_provider

T0 = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
PROVS = ["codex", "claude"]

STATES = ["ok", "limited", "expired", "model", "disabled"]
ALLOWED = [None, ["codex"], ["claude"], ["codex", "claude"], []]
MODELS = [None, {"codex": "gpt", "claude": "claude-opus-4"}]


def mk(state):
    s = ProviderStatus()
    if state == "limited":
        s.limited_until, s.window = T0 + 2 * H, "five_hour"
    elif state == "expired":
        s.limited_until, s.window = T0 - H, "five_hour"
    elif state == "model":
        s.model_limits = {"opus": T0 + 3 * H}
    elif state == "disabled":
        s.disabled = "auth error"
    return s


def hours(h):
    return None if h is None else int((h - T0) / H)

PICK_Z = (
    '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'
    'LIYSTQ=='
)

CHOOSE_Z = (
    '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'
    'SqUdGPnX/wHJnKfs'
)

PICK = ast.literal_eval(zlib.decompress(base64.b64decode(''.join(PICK_Z))).decode())
CHOOSE = ast.literal_eval(zlib.decompress(base64.b64decode(''.join(CHOOSE_Z))).decode())



class SchedulerGridTest(unittest.TestCase):
    def test_pick(self):
        grid = itertools.product(PROVS, STATES, STATES, ALLOWED, [False, True], [True, False], MODELS)
        for (cur, sc, sl, allowed, fresh, ret, models), want in zip(grid, PICK, strict=True):
            s = Scheduler(PROVS, cur, {"codex": mk(sc), "claude": mk(sl)}, return_to_preferred=ret)
            p = s.pick(T0, allowed, fresh=fresh, models=models)
            after = {k: (hours(v.limited_until), tuple(sorted(v.model_limits)), v.window) for k, v in s.status.items()}
            got = (cur, sc, sl, allowed, fresh, ret, bool(models),
                   (p.provider, hours(p.wait_until), p.switched_from, p.restricted_from, p.reason), s.current, after)
            with self.subTest(case=want[:7]):
                self.assertEqual(got, want)

    def test_choose_provider(self):
        grid = itertools.product(STATES, STATES, ALLOWED, [None, "codex", "claude"],
                                 [{}, {"codex": 1}, {"claude": 1, "codex": 1}], MODELS)
        for (sc, sl, allowed, last, busy, models), want in zip(grid, CHOOSE, strict=True):
            st = {"codex": mk(sc), "claude": mk(sl)}
            p = choose_provider(PROVS, st, allowed, last, T0, busy, {"codex": 1, "claude": 1}, models)
            got = (sc, sl, allowed, last, tuple(sorted(busy)), bool(models),
                   (p.provider, hours(p.wait_until), p.switched_from, p.restricted_from, p.reason, p.slot_busy))
            with self.subTest(case=want[:6]):
                self.assertEqual(got, want)

    def test_the_grids_have_the_expected_size(self):
        self.assertEqual(len(PICK), 2 * 25 * 5 * 2 * 2 * 2)
        self.assertEqual(len(CHOOSE), 25 * 5 * 3 * 3 * 2)


if __name__ == "__main__":
    unittest.main()
