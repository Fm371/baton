"""The --help text of baton and of every subcommand (pins the argument parser so it can be
restructured). Generated from the code before the refactor; zlib+base64 of repr(dict)."""

import ast
import base64
import contextlib
import io
import os
import unittest
import zlib
from unittest import mock

from baton import cli

COMMANDS = [[], ['review'], ['review-fix'], ['init'], ['run'], ['plan'], ['stop'], ['watch'], ['status'], ['approve'], ['reject'], ['mark'], ['retry'], ['providers'], ['providers', 'reset'], ['classify']]

HELP_Z = (
    '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'
)
HELP = ast.literal_eval(zlib.decompress(base64.b64decode(''.join(HELP_Z))).decode())



def helptext(cmd):
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"COLUMNS": "100"}), contextlib.redirect_stdout(out), \
            contextlib.suppress(SystemExit):
        cli.main([*cmd, "-h"])
    return out.getvalue()


class HelpTest(unittest.TestCase):
    def test_every_help_text_is_as_before(self):
        self.assertEqual(sorted(HELP), sorted(" ".join(c) or "baton" for c in COMMANDS))
        for cmd in COMMANDS:
            with self.subTest(cmd=" ".join(cmd) or "baton"):
                self.assertEqual(helptext(cmd), HELP[" ".join(cmd) or "baton"])


if __name__ == "__main__":
    unittest.main()
