import contextlib
import io
import re
import tomllib
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import baton
from baton import cli

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def test_the_package_version_matches_pyproject(self):
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        try:
            installed = version("baton-runner")
        except PackageNotFoundError:
            installed = declared            # not installed: __version__ falls back to pyproject
        self.assertEqual(baton.__version__, installed)

    def test_the_changelog_has_the_current_release(self):
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        releases = re.findall(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.MULTILINE)
        self.assertEqual(releases[0], declared)

    def test_dash_dash_version_prints_it(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as e:
            cli.main(["--version"])
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), f"baton {baton.__version__}")
