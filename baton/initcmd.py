"""`baton init [DIR]`: write a ready-to-edit workspace (baton.toml, common.md, an example task and
the example Claude settings) from the templates that ship inside the package."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

TEMPLATE = ("templates", "task-auto")
SKIP = {"__pycache__"}


class InitError(Exception):
    """The folder cannot be initialised (not empty, not a folder, ...)."""


def template_root() -> Traversable:
    root = resources.files("baton")
    for part in TEMPLATE:
        root = root / part
    return root


def _walk(node: Traversable, prefix: Path = Path()):
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        if child.name in SKIP or child.name.endswith(".pyc"):
            continue
        rel = prefix / child.name
        if child.is_dir():
            yield from _walk(child, rel)
        else:
            yield rel, child


def template_files() -> list[Path]:
    """Relative paths of every file in the workspace template."""
    return [rel for rel, _ in _walk(template_root())]


def init_workspace(dest: Path, force: bool = False) -> list[Path]:
    """Copy the template into `dest`. A folder that already holds anything is refused unless `force`
    (then files of the template are overwritten and nothing else is touched). Returns the files written."""
    if dest.exists() and not dest.is_dir():
        raise InitError(f"{dest} exists and is not a folder")
    if dest.is_dir() and any(dest.iterdir()) and not force:
        raise InitError(f"{dest} is not empty; pick another folder or pass --force to overwrite the template "
                        "files in it (nothing else is touched)")
    written = []
    for rel, src in _walk(template_root()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
        written.append(rel)
    return written


NEXT_STEPS = """\
Created {dest} ({n} files).

Next:
  1. cd {dest}
  2. cp claude-settings.example.json claude-settings.json   # then edit the deny rules for your project
  3. edit baton.toml: the providers you use, [run] default_base, the models in [routing]
  4. point tasks/10-example.md at a real, clean git repository (repo = "../my-repo" is relative to baton.toml)
     and write your own tasks next to it
  5. baton plan      # checks everything and shows the order; runs nothing
  6. baton run       # inside tmux, with the machine kept awake (see the README: Before you launch)
"""
