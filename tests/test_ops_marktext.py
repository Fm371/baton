"""What `baton mark` writes into results/<id>.md, case by case (pins the text so it can be restructured)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from baton.ops import OpsError, mark_text


def task(runs_agent=True):
    return SimpleNamespace(runs_agent=runs_agent, id="t1")


class MarkTextTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.result = self.dir / "t1.md"

    def test_no_result_file_gets_a_one_line_note(self):
        self.assertEqual(mark_text(task(), "done", "checked by hand", None, self.result),
                         "Marked done by the operator: checked by hand\n")
        self.assertEqual(mark_text(task(), "blocked", "", None, self.result),
                         "Marked blocked by the operator.\n")

    def test_failed_and_non_agent_tasks_write_nothing(self):
        self.assertIsNone(mark_text(task(), "failed", "x", None, self.result))
        self.assertIsNone(mark_text(task(runs_agent=False), "done", "x", None, self.result))

    def test_an_existing_result_gets_its_first_line_rewritten(self):
        self.result.write_text("STATUS: partial\nSome notes.\n")
        self.assertEqual(mark_text(task(), "done", "ok now", None, self.result),
                         "STATUS: done\nSome notes.\n\n(Status set to done by the operator: ok now; "
                         "the agent had written: STATUS: partial.)\n")
        self.assertEqual(mark_text(task(), "blocked", "", None, self.result),
                         "STATUS: blocked\nSome notes.\n\n(Status set to blocked by the operator; "
                         "the agent had written: STATUS: partial.)\n")

    def test_a_result_that_already_says_so_or_has_no_status_line_is_left_alone(self):
        self.result.write_text("STATUS: done\nfine\n")
        self.assertIsNone(mark_text(task(), "done", "", None, self.result))
        self.result.write_text("no status line here\n")
        self.assertIsNone(mark_text(task(), "done", "", None, self.result))
        self.result.write_text("STATUS: partial\nx\n")
        self.assertIsNone(mark_text(task(), "failed", "", None, self.result))

    def test_a_result_file_argument_wins_and_must_exist(self):
        given = self.dir / "mine.md"
        given.write_text("my own text\n")
        self.assertEqual(mark_text(task(), "failed", "", given, self.result), "my own text\n")
        with self.assertRaises(OpsError):
            mark_text(task(), "done", "", self.dir / "missing.md", self.result)


if __name__ == "__main__":
    unittest.main()
