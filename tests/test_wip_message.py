import unittest
from pathlib import Path

from baton.runner import Runner
from baton.tasks import Task


def task(tid):
    return Task(id=tid, path=Path("x.md"), repo=Path("."), body="b")


class WipMessageTest(unittest.TestCase):
    def test_uses_an_allowed_commit_type(self):
        self.assertEqual(Runner.wip_message(task("a"), "claude hit its usage limit"),
                         "chore(a): wip, claude hit its usage limit (unverified)")

    def test_header_is_cut_to_100_characters(self):
        msg = Runner.wip_message(task("long-task-id"), "the previous attempt ended without a result file " * 5)
        self.assertEqual(len(msg), 100)
        self.assertTrue(msg.startswith("chore(long-task-id): wip, "))
        self.assertTrue(msg.endswith("… (unverified)"))


if __name__ == "__main__":
    unittest.main()
