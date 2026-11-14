import re
import unittest
from pathlib import Path

from baton.config import load_config
from baton.tasks import load_tasks

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "baton" / "templates" / "task-auto"

# a user's home folder spelled out, e.g. /home/<name>/ or /Users/<name>/
HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/")


class ExampleWorkspaceTest(unittest.TestCase):
    def test_example_config_and_task_load(self):
        cfg = load_config(EXAMPLE / "baton.toml")
        tasks = load_tasks(cfg)
        self.assertEqual([t.id for t in tasks], ["10-example"])
        self.assertIn(tasks[0].resolved_rung, cfg.routing.rungs)

    def test_no_home_paths_in_shipped_files(self):
        found = []
        for path in ROOT.rglob("*"):
            parts = path.relative_to(ROOT).parts
            if not path.is_file() or path == Path(__file__).resolve():
                continue
            if parts[0] in {".git", ".claude", "build", "dist"} or "__pycache__" in parts or parts[0].endswith(".egg-info"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            found += [f"{path.relative_to(ROOT)}: {m.group(0)}" for m in HOME_PATH.finditer(text)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
