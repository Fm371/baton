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
