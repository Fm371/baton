import contextlib
import fnmatch
import io
import tempfile
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from baton import cli
from baton.config import load_config
from baton.initcmd import InitError, init_workspace, template_files
from baton.tasks import load_tasks

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"baton.toml", "common.md", "claude-settings.example.json", "tasks/10-example.md"}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_template_has_the_expected_files(self):
        self.assertEqual({p.as_posix() for p in template_files()}, EXPECTED)

    def test_init_writes_a_workspace_that_loads(self):
        dest = self.tmp / "task-auto"
        rc, out, _ = run_cli("init", str(dest))
        self.assertEqual(rc, 0)
        for name in EXPECTED:
            self.assertTrue((dest / name).is_file(), name)
        self.assertFalse((dest / "claude-settings.json").exists())      # only the .example: the user edits a copy
        self.assertIn("baton plan", out)
        self.assertIn("claude-settings.example.json", out)
        cfg = load_config(dest / "baton.toml")
        self.assertEqual([t.id for t in load_tasks(cfg)], ["10-example"])

    def test_a_folder_that_is_not_empty_is_refused(self):
        dest = self.tmp / "w"
        dest.mkdir()
        (dest / "mine.txt").write_text("keep")
        rc, _, err = run_cli("init", str(dest))
        self.assertEqual(rc, 2)
        self.assertIn("not empty", err)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["mine.txt"])      # nothing written

    def test_force_overwrites_template_files_and_keeps_the_rest(self):
        dest = self.tmp / "w"
        dest.mkdir()
        (dest / "mine.txt").write_text("keep")
        (dest / "baton.toml").write_text("# edited\n")
        rc, _, _ = run_cli("init", str(dest), "--force")
        self.assertEqual(rc, 0)
        self.assertEqual((dest / "mine.txt").read_text(), "keep")
        self.assertNotEqual((dest / "baton.toml").read_text(), "# edited\n")

    def test_an_empty_existing_folder_is_fine(self):
        dest = self.tmp / "empty"
        dest.mkdir()
        self.assertEqual(run_cli("init", str(dest))[0], 0)

    def test_a_file_in_the_way_is_refused(self):
        f = self.tmp / "file"
        f.write_text("x")
        with self.assertRaises(InitError):
            init_workspace(f)
        self.assertEqual(run_cli("init", str(f))[0], 2)

    def test_default_folder_is_task_auto_in_the_working_directory(self):
        import os
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            self.assertEqual(run_cli("init")[0], 0)
        finally:
            os.chdir(old)
        self.assertTrue((self.tmp / "task-auto" / "baton.toml").is_file())


class PackageDataTest(unittest.TestCase):
    """The wheel must carry the templates: pyproject's package-data has to match every template file."""

    def test_package_data_patterns_cover_every_template_file(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["baton"]
        base = resources.files("baton") / "templates"
        self.assertTrue(base.is_dir())
        root = ROOT / "baton"
        files = [p.relative_to(root).as_posix() for p in (root / "templates").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts]
        self.assertEqual(len(files), len(EXPECTED))
        for f in files:
            # setuptools' `*` does not cross a folder, fnmatch's does: compare the depth as well
            self.assertTrue(any(fnmatch.fnmatchcase(f, pat) and pat.count("/") == f.count("/") for pat in data),
                            f"not in package-data: {f}")

    def test_templates_are_not_a_stray_python_package(self):
        self.assertFalse((ROOT / "baton" / "templates" / "__init__.py").exists())


if __name__ == "__main__":
    unittest.main()
