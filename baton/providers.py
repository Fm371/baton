"""Command lines for each CLI. The prompt always goes in on stdin: flags such
as --add-dir take several values and would swallow a trailing prompt."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import ProviderConfig, RungSpec


# `[providers.codex] network = true`: commands inside the workspace-write sandbox may use the network
CODEX_NETWORK_ARGS = ("-c", "sandbox_workspace_write.network_access=true")


def _dirs_args(pc: ProviderConfig, add_dirs: list[str] | None) -> list[str]:
    argv: list[str] = []
    for d in (pc.add_dirs if add_dirs is None else add_dirs):
        argv += ["--add-dir", d]
    return argv


def _claude_command(pc: ProviderConfig, spec: RungSpec, max_turns: int | None, settings: str | None,
                    add_dirs: list[str] | None, env: dict[str, str]) -> list[str]:
    argv = [pc.bin, "-p", "--output-format", "stream-json", "--verbose", "--model", spec.model]
    if spec.effort:
        if pc.effort_flag:
            argv += ["--effort", spec.effort]
        env["CLAUDE_CODE_EFFORT_LEVEL"] = spec.effort
    if pc.permission_mode:
        argv += ["--permission-mode", pc.permission_mode]
    settings = settings or pc.settings
    if settings:
        argv += ["--settings", settings]
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    return argv + _dirs_args(pc, add_dirs) + pc.extra_args


def _codex_command(pc: ProviderConfig, spec: RungSpec, max_turns: int | None, settings: str | None,
                   add_dirs: list[str] | None, env: dict[str, str]) -> list[str]:
    argv = [pc.bin, "exec", "--json"]
    if pc.profile:
        argv += ["--profile", pc.profile]
    if pc.sandbox:
        argv += ["--sandbox", pc.sandbox]
    argv += _dirs_args(pc, add_dirs)
    argv += ["--model", spec.model]
    if spec.effort:
        argv += ["-c", f"model_reasoning_effort={spec.effort}"]
    if pc.network:
        argv += list(CODEX_NETWORK_ARGS)
    return argv + pc.extra_args + ["-"]            # "-": read the prompt from stdin


# provider name -> how its command line is built; a new CLI needs one entry here
COMMAND_BUILDERS = {"claude": _claude_command, "codex": _codex_command}


def build_command(pc: ProviderConfig, spec: RungSpec, max_turns: int | None,
                  settings: str | None = None, add_dirs: list[str] | None = None,
                  ) -> tuple[list[str], dict[str, str]]:
    """settings and add_dirs override the provider's own values."""
    env = dict(pc.env)
    builder = COMMAND_BUILDERS.get(pc.name)
    if builder is None:
        raise ValueError(pc.name)
    return builder(pc, spec, max_turns, settings, add_dirs, env), env


def resolve_bin(pc: ProviderConfig) -> str | None:
    if os.sep in pc.bin:
        return pc.bin if os.path.isfile(pc.bin) and os.access(pc.bin, os.X_OK) else None
    return shutil.which(pc.bin)


def home_dir(pc: ProviderConfig) -> Path | None:
    """Where the CLI keeps its session files, for the limit fallback."""
    if pc.home:
        return Path(pc.home)
    if pc.name == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if pc.name == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return None
