"""Without a start_provider line a run begins on Codex, or on Claude when Codex is disabled."""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from baton.config import load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "baton" / "templates" / "task-auto"


class DefaultStartProviderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(EXAMPLE / "common.md", self.dir / "common.md")
        text = (EXAMPLE / "baton.toml").read_text()
        self.base = re.sub(r"(?m)^start_provider\s*=.*\n", "", text)
        self.assertNotIn("start_provider =", self.base)

    def load(self, text):
        path = self.dir / "baton.toml"
        path.write_text(text)
        return load_config(path)

    def test_codex_by_default(self):
        self.assertEqual(self.load(self.base).start_provider, "codex")

    def test_claude_when_codex_disabled(self):
        text = self.base.replace("[providers.codex]\n", "[providers.codex]\nenabled = false\n", 1)
        self.assertEqual(self.load(text).start_provider, "claude")

    def test_explicit_value_wins(self):
        text = self.base.replace("[run]\n", '[run]\nstart_provider = "claude"\n', 1)
        self.assertEqual(self.load(text).start_provider, "claude")

    def test_example_config_starts_on_codex(self):
        self.assertEqual(load_config(EXAMPLE / "baton.toml").start_provider, "codex")


if __name__ == "__main__":
    unittest.main()
