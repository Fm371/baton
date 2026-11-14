"""baton: run a folder of coding-agent tasks unattended, handing the work
between Claude Code and Codex when one of them hits its usage limit."""

from importlib.metadata import PackageNotFoundError, version as _installed_version
from pathlib import Path


def _version() -> str:
    """The installed distribution's version; from pyproject.toml when run from a
    source checkout that is not installed."""
    try:
        return _installed_version("baton-runner")
    except PackageNotFoundError:
        pass
    try:
        import tomllib
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, ValueError):
        return "unknown"


__version__ = _version()
