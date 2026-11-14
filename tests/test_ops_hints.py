"""The hints `baton status` prints, case by case (pins the wording so the function can be restructured)."""

import unittest
from types import SimpleNamespace as NS

from baton.ops import hints


class State:
    def __init__(self, **statuses):
        self.t = {k: NS(status=v[0], rejected_by=v[1] if len(v) > 1 else None,
                        gated_by=v[2] if len(v) > 2 else None) for k, v in statuses.items()}

    def task(self, tid):
        return self.t[tid]


def task(tid, approval=False):
    return NS(id=tid, is_approval=approval)


class HintsTest(unittest.TestCase):
    def test_each_kind_of_stuck_task_gets_its_command(self):
        ordered = [task("ck", approval=True), task("a"), task("v"), task("b"), task("r", approval=True)]
        st = State(ck=("awaiting",), a=("partial",), v=("failed",), b=("gated", None, "v"), r=("rejected",))
        self.assertEqual(hints(ordered, st, active=True), [
            "ck waits for you: read results/ck.md, then `baton approve ck` or `baton reject ck --note ...`",
            "a is partial: `baton retry a` runs it again; if the work is fine as it is (the cause was elsewhere), "
            "`baton mark a done --note \"why\"`",
            "v is failed: `baton retry v` runs it again; if the work is fine as it is (the cause was elsewhere), "
            "`baton mark v done --note \"why\"`",
            "b is held by verifier v (failed): fix the cause and `baton retry v`, or accept it with "
            "`baton mark v done --note \"why\"`",
            "r was rejected: `baton approve r` releases what it holds",
        ])

    def test_a_rejected_task_is_not_offered_a_retry_and_a_missing_gate_is_ignored(self):
        ordered = [task("a"), task("b")]
        st = State(a=("blocked", "ck"), b=("skipped", None, "ghost"))
        self.assertEqual(hints(ordered, st, active=True), [])

    def test_run_continues_hint_only_when_idle_and_work_is_left(self):
        ordered = [task("a")]
        self.assertEqual(hints(ordered, State(a=("pending",)), active=False), ["`baton run` continues the run"])
        self.assertEqual(hints(ordered, State(a=("pending",)), active=True), [])
        self.assertEqual(hints(ordered, State(a=("done",)), active=False), [])

    def test_duplicates_are_dropped_and_the_list_is_capped(self):
        ordered = [task(f"t{i}") for i in range(20)]
        st = State(**{f"t{i}": ("failed",) for i in range(20)})
        out = hints(ordered, st, active=True)
        self.assertEqual(len(out), 12)
        self.assertEqual(len(set(out)), 12)


if __name__ == "__main__":
    unittest.main()
