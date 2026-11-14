"""The documentation must keep up with the code: every config key and task-header key is in the reference,
every subcommand is documented, every relative link and anchor resolves, and the README stays short."""

import ast
import re
import unittest
from pathlib import Path

from baton import config as cfg_mod
from baton import tasks as tasks_mod

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
MAX_README_LINES = 250


def doc_files() -> list[Path]:
    return [README] + sorted(DOCS.rglob("*.md")) + [ROOT / "CHANGELOG.md"]


def literal_set_assigned_in(source_path: Path, function: str, name: str) -> set[str]:
    """The strings of `name = {...}` inside `function` of a module (for keys that are local to a function)."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == function:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
                        and isinstance(node.value, ast.Set)):
                    return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError(f"{function}() in {source_path.name} has no set literal called {name}")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    out, in_code = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"#{1,6} ", line):
            out.add(slug(line.lstrip("#")))
    return out


def without_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


class ReferenceCoversEveryKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = (DOCS / "configuration.md").read_text(encoding="utf-8")

    def assert_documented(self, keys, where):
        missing = sorted(k for k in keys if f"`{k}`" not in self.reference)
        self.assertEqual(missing, [], f"{where} keys missing from docs/configuration.md")

    def test_run_keys(self):
        self.assert_documented(cfg_mod.RUN_KEYS, "[run]")

    def test_provider_keys(self):
        self.assert_documented(cfg_mod.PROVIDER_KEYS, "[providers.*]")

    def test_service_keys(self):
        self.assert_documented(cfg_mod.SERVICE_KEYS, "[services.*]")

    def test_repo_keys(self):
        self.assert_documented(cfg_mod.REPO_KEYS, '[repos."path"]')

    def test_preflight_keys(self):
        keys = literal_set_assigned_in(ROOT / "baton" / "config.py", "_load_preflight", "known")
        self.assert_documented(keys, "[preflight]")

    def test_top_level_sections(self):
        for section in cfg_mod.TOP_LEVEL_KEYS:
            self.assertIn(f"[{section}", self.reference, f"section [{section}] is not in docs/configuration.md")

    def test_task_front_matter_keys(self):
        keys = tasks_mod.TASK_KEYS
        self.assertGreater(len(keys), 15)
        self.assert_documented(keys, "task front matter")


class CommandsAreDocumentedTest(unittest.TestCase):
    def test_every_subcommand_is_in_the_readme_or_operations(self):
        source = (ROOT / "baton" / "cli.py").read_text(encoding="utf-8")
        commands = re.findall(r"\bsub\.add_parser\(\s*\"([a-z-]+)\"", source)
        self.assertGreater(len(commands), 8)
        text = README.read_text(encoding="utf-8") + (DOCS / "operations.md").read_text(encoding="utf-8")
        missing = [c for c in commands if f"baton {c}" not in text]
        self.assertEqual(missing, [], "subcommands not shown as `baton <cmd>` in README.md or docs/operations.md")


class LinksTest(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        problems = []
        for path in doc_files():
            text = without_code(path.read_text(encoding="utf-8"))
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+:", target):          # http:, https:, mailto:
                    continue
                file_part, _, anchor = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                if not dest.exists():
                    problems.append(f"{path.relative_to(ROOT)}: {target} -> no such file")
                elif anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
                    problems.append(f"{path.relative_to(ROOT)}: {target} -> no such heading")
        self.assertEqual(problems, [])


class ReadmeTest(unittest.TestCase):
    def test_readme_is_short(self):
        lines = README.read_text(encoding="utf-8").count("\n")
        self.assertLess(lines, MAX_README_LINES,
                        f"README.md has {lines} lines; put the detail in docs/ and keep the README an entry point")

    def test_every_docs_page_is_in_the_index(self):
        index = (DOCS / "README.md").read_text(encoding="utf-8")
        pages = [p.name for p in DOCS.glob("*.md") if p.name != "README.md"]
        self.assertGreater(len(pages), 8)
        self.assertEqual([p for p in pages if f"({p})" not in index], [])

    def test_readme_links_to_the_quick_start(self):
        self.assertIn("docs/quickstart.md", README.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
