"""Keeping verifier files away from implementers, and implementer output away
from verifiers.

Claude gets a per-attempt settings file: the user's settings plus deny rules
for the hidden paths. Read rules also cover Grep and Glob; Bash rules match
the absolute path anywhere in a command, which is a best effort (a relative
path after `cd` is not caught). Codex has no read restriction, so for Codex
the only protection is that the hidden paths never appear in its prompt or
command line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .pathcheck import expand
from .tasks import Task


def hidden_paths(cfg: Config, task: Task, verifiers: list[Task]) -> list[Path]:
    """What this task's agent must not read."""
    if task.is_verifier:
        # A fresh look: no task files and nothing another task left in results/.
        # Its own result file, notes and attempt folder stay open: a deny on the
        # whole folder also blocks the verifier from writing results/<id>.md.
        own = {f"{task.id}.md", f"{task.id}.progress.md", task.id}
        results = cfg.results_dir.resolve()
        others = sorted(p.resolve() for p in results.iterdir() if p.name not in own) if results.is_dir() else []
        return [cfg.tasks_dir.resolve(), *others]
    paths = [cfg.verifiers_dir.resolve()]
    for v in verifiers:
        paths += [(cfg.results_dir / v.id).resolve(), (cfg.results_dir / f"{v.id}.md").resolve(),
                  (cfg.results_dir / f"{v.id}.progress.md").resolve()]
    return paths


def deny_rules(paths: list[Path]) -> list[str]:
    rules: list[str] = []
    for p in paths:
        # "//" starts an absolute path in Claude Code permission rules
        rules += [f"Read(/{p})", f"Read(/{p}/**)", f"Bash(*{p}*)"]
    return rules


def write_claude_settings(user_settings: str | None, rules: list[str], dest: Path,
                          base_dir: Path | None = None) -> Path:
    """The user's settings file with the deny rules added; {base_dir} in it becomes the baton folder."""
    data: dict = {}
    if user_settings:
        text = Path(user_settings).read_text(encoding="utf-8")
        if base_dir is not None:
            text = expand(text, base_dir)
        data = json.loads(text)
    # no agent-credit trailers in the agents' commits (a run's common.md usually forbids them, and
    # Claude Code's own attribution would contradict it); a user's own choice in the file stays
    data.setdefault("includeCoAuthoredBy", False)
    data.setdefault("attribution", {"commit": "", "pr": ""})
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    for r in rules:
        if r not in deny:
            deny.append(r)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def with_results_dir(dirs: list[str], results_dir: Path, repo: Path) -> list[str]:
    """Claude runs in the repository, but the agent writes results/<id>.md (and its
    progress notes) next to baton.toml, outside it. Without --add-dir for that
    folder Claude refuses the write in headless mode and every task ends
    "without a result file". Add it unless it is already covered."""
    target = results_dir.resolve()
    for d in [*dirs, str(repo)]:
        r = Path(d).resolve()
        if r == target or r in target.parents:
            return list(dirs)
    return [*dirs, str(target)]


def visible_dirs(dirs: list[str], hidden: list[Path]) -> list[str]:
    """add_dirs entries that are not themselves hidden or inside a hidden path."""
    out = []
    for d in dirs:
        r = Path(d).resolve()
        if not any(r == h or h in r.parents for h in hidden):
            out.append(d)
    return out


def _glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def outside_test_paths(paths: list[str], patterns: list[str]) -> list[str]:
    """The changed paths that match none of the test-path globs."""
    compiled = [_glob_re(p) for p in patterns]
    return [p for p in paths if not any(c.match(p) for c in compiled)]


def matches_any(path: str, patterns: list[str]) -> bool:
    """True when `path` matches a glob. A pattern without a slash (`*.tsx`)
    is matched against the file name at any depth."""
    for pattern in patterns:
        if "/" not in pattern:
            if _glob_re(pattern).match(path.rsplit("/", 1)[-1]):
                return True
        elif _glob_re(pattern).match(path):
            return True
    return False
