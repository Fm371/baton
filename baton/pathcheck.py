"""Absolute paths in the files baton hands to an agent, and the `{base_dir}` placeholder.

A baton folder is easy to move (a new name, another machine, a different user). What breaks
silently is every absolute path inside it: in common.md, in task files, and in the allow/deny
rules of claude-settings.json (a deny rule for `/old/path/.env` just stops protecting anything).
`{base_dir}` is replaced with the folder of baton.toml wherever it appears in those files, so
they can be written without hard-coded paths; `plan` warns about the absolute paths that remain
and do not exist.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import Config

PLACEHOLDER = "{base_dir}"
# an absolute or home-relative path with at least two components, as it appears in prose,
# a task body, or inside a settings rule such as Read(//srv/app/.env)
PATH_RE = re.compile(r"(?<![\w./:~-])(~/|/)([\w@.+-]+(?:/[\w@.+-]+)+)")
TRAILING = ".,;:)]}'\"`"


def expand(text: str, base_dir: Path) -> str:
    """Replace {base_dir} with the folder of baton.toml."""
    return text.replace(PLACEHOLDER, str(base_dir)) if PLACEHOLDER in text else text


def has_placeholder(path: str | os.PathLike | None) -> bool:
    if not path:
        return False
    try:
        return PLACEHOLDER in Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _candidates(text: str) -> list[str]:
    """Absolute paths in free text. A path counts only when its first component is a real
    top-level folder on this machine (for example /opt or /srv): `/api/plans` in a task body is
    an URL path, not a file, and must not be reported."""
    found: list[str] = []
    for m in PATH_RE.finditer(text):
        root, rest = m.group(1), m.group(2).rstrip(TRAILING)
        if "*" in rest or "{" in rest:
            rest = re.split(r"[*{]", rest)[0].rstrip("/")
        if "/" not in rest and root == "/":
            continue
        first = rest.split("/")[0]
        if root == "/" and not os.path.isdir("/" + first):
            continue
        found.append(root + rest)
    return list(dict.fromkeys(found))


def moved_hint(cfg: Config, path: str) -> str:
    """" (use {base_dir}: ...)" when the same file exists under the baton folder, which is what a moved
    baton folder looks like. A path that is deliberately outside the workspace gets no hint."""
    parts = Path(os.path.expanduser(path)).parts[1:]
    for i in range(len(parts)):
        mine = cfg.base_dir.joinpath(*parts[i:])
        if i and mine.exists():
            return f" (it exists at {mine}; write it with {{base_dir}} to follow the baton folder)"
    return ""


def missing_paths(text: str) -> list[str]:
    out = []
    for p in _candidates(text):
        if not os.path.exists(os.path.expanduser(p)):
            out.append(p)
    return out


def _settings_rules(path: str) -> tuple[list[tuple[str, str]], str | None]:
    """(kind, rule) pairs of a claude settings file: kind is allow, deny or dir."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], f"cannot read {path}: {e}"
    perms = data.get("permissions", {}) if isinstance(data, dict) else {}
    rules = [(k, r) for k in ("allow", "deny") for r in perms.get(k, []) if isinstance(r, str)]
    rules += [("dir", d) for d in perms.get("additionalDirectories", []) if isinstance(d, str)]
    return rules, None


_RULE_RE = re.compile(r"\s*([A-Za-z_][\w.-]*)\((.*)\)\s*", re.S)


def _rule_paths(rule: str) -> list[str]:
    """Absolute paths inside a permission rule `Tool(specifier)`. Claude writes an absolute path as
    `//abs/path`; a Bash specifier is a command prefix, `Bash(/usr/bin/ruff:*)`, whose `:*` or ` *`
    suffix is the wildcard, not part of the path. Relative paths (`.venv/bin/python`) are skipped:
    they mean something different in every task repository."""
    m = _RULE_RE.fullmatch(rule)
    spec = m.group(2) if m else rule
    tokens = spec.split() if (not m or m.group(1) == "Bash") else [spec.strip()]
    out = []
    for tok in tokens:
        lead = "//" if tok.startswith("//") else "~/" if tok.startswith("~/") else "/" if tok.startswith("/") else ""
        if not lead:
            continue
        rest = re.split(r"[*?\[\]{}()]", tok[len(lead):])[0]
        rest = rest.rstrip(":").rstrip("/")
        if not rest or ("/" not in rest and lead == "/"):    # `/etc`-style single component: not project-specific
            continue
        out.append(("/" if lead == "//" else lead) + rest)
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


_GONE = {
    "deny": "a deny rule for a path that is gone protects nothing",
    "allow": "an allow rule for a path that is gone permits nothing (is the tool installed somewhere else now?)",
    "dir": "an additional directory that is gone gives the agent nothing to read or write",
}


def settings_warnings(cfg: Config, settings_path: str, owner: str, repos: list[Path]) -> list[str]:
    """Paths a claude settings file mentions that do not exist, and write access granted outside
    the baton folder and the task repositories."""
    rules, err = _settings_rules(settings_path)
    if err:
        return []
    warnings: list[str] = []
    seen: set[str] = set()
    roots = [cfg.base_dir.resolve(), *[r.resolve() for r in repos]]
    for kind, rule in rules:
        for raw in _rule_paths(rule) if kind != "dir" else [rule]:
            expanded = expand(raw, cfg.base_dir)
            if PLACEHOLDER in expanded:
                continue
            p = Path(os.path.expanduser(expanded))
            if not p.is_absolute():
                continue
            key = f"{kind}:{p}"
            if key in seen:
                continue
            seen.add(key)
            if not p.exists():
                warnings.append(f"{owner}: the settings file mentions {p}, which does not exist; "
                                + _GONE[kind] + moved_hint(cfg, str(p)))
            elif (kind == "dir" or (kind == "allow" and rule.startswith(("Edit", "Write")))) \
                    and not _inside(p.resolve(), roots):
                warnings.append(f"{owner}: the settings file lets the agent write to {p}, outside the "
                                "baton folder and the task repositories")
    return warnings


def _task_text_warnings(tasks: list) -> list[str]:
    """Missing absolute paths in task bodies. A path an earlier task (one this task depends on) names is
    taken to be made by it."""
    from .tasks import ancestors        # not at import time: tasks imports this module
    by_id = {t.id: t for t in tasks}
    out: list[str] = []
    for t in tasks:
        earlier = [a.body for a in ancestors(t, by_id)]
        out += [f"task {t.id}: path {p} in the task text does not exist" for p in missing_paths(t.body)
                if not any(p in text or os.path.dirname(p) + "/" in text for text in earlier)]
    return out


def path_warnings(cfg: Config, tasks: list) -> list[str]:
    """Warnings for `baton plan`: absolute paths that do not exist in common.md, in the task files
    and in the claude settings file."""
    warnings: list[str] = []
    if cfg.common_prompt and cfg.common_prompt.is_file():
        text = expand(cfg.common_prompt.read_text(encoding="utf-8"), cfg.base_dir)
        warnings += [f"{cfg.common_prompt.name}: path {p} does not exist{moved_hint(cfg, p)}"
                     for p in missing_paths(text)]
    warnings += _task_text_warnings(tasks)
    repos = [t.repo for t in tasks if getattr(t, "repo", None)]
    for name, pc in cfg.providers.items():
        if name == "claude" and pc.enabled and pc.settings and os.path.isfile(pc.settings):
            warnings += settings_warnings(cfg, pc.settings, f"{name}", repos)
    return warnings
