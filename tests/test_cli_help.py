"""The --help text of baton and of every subcommand (pins the argument parser so it can be
restructured). Generated from the code before the refactor; zlib+base64 of repr(dict)."""

import argparse
import ast
import base64
import contextlib
import io
import os
import unittest
import zlib
from unittest import mock

from baton import cli

COMMANDS = [[], ['review'], ['review-fix'], ['init'], ['run'], ['plan'], ['stop'], ['watch'], ['status'], ['approve'], ['reject'], ['mark'], ['retry'], ['resume'], ['providers'], ['providers', 'reset'], ['classify']]

HELP_Z = (
    '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'
)
HELP = ast.literal_eval(zlib.decompress(base64.b64decode(''.join(HELP_Z))).decode())



def _invocation_313(self, action):
    """argparse's option column as Python 3.13+ writes it ("-c, --config CONFIG"); older versions
    repeat the metavar ("-c CONFIG, --config CONFIG"), which also widens the column."""
    if not action.option_strings:
        default = self._get_default_metavar_for_positional(action)
        return " ".join(self._metavar_formatter(action, default)(1))
    if action.nargs == 0:
        return ", ".join(action.option_strings)
    default = self._get_default_metavar_for_optional(action)
    return ", ".join(action.option_strings) + " " + self._format_args(action, default)


def _same_usage_wrapping(text):
    """Python 3.13 wraps a long usage line differently (where "..." lands); collapse the
    whitespace of the usage block only, so its words are still compared."""
    usage, sep, rest = text.partition("\n\n")
    return " ".join(usage.split()) + sep + rest


def helptext(cmd):
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"COLUMNS": "100"}), contextlib.redirect_stdout(out), \
            mock.patch.object(argparse.HelpFormatter, "_format_action_invocation", _invocation_313), \
            contextlib.suppress(SystemExit):
        cli.main([*cmd, "-h"])
    return _same_usage_wrapping(out.getvalue())


class HelpTest(unittest.TestCase):
    def test_every_help_text_is_as_before(self):
        self.assertEqual(sorted(HELP), sorted(" ".join(c) or "baton" for c in COMMANDS))
        for cmd in COMMANDS:
            with self.subTest(cmd=" ".join(cmd) or "baton"):
                self.assertEqual(helptext(cmd), _same_usage_wrapping(HELP[" ".join(cmd) or "baton"]))


if __name__ == "__main__":
    unittest.main()
