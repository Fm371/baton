"""env-preflight: what a human would trip over before manual QA.

Read only. For each repo in `[preflight] repos`:
  * every key in `.env.example` is set (not empty) in a real env file; only
    key names are printed, never values;
and across the workspace:
  * the backend's CORS list contains every panel origin;
  * the auth redirect allow-list contains every panel origin;
  * `localhost` and `127.0.0.1` are not mixed across the callback URL, the
    panel origins and the CORS list (a callback that sets host-only cookies on
    127.0.0.1 never reaches a panel opened on localhost).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import CheckContext, CheckResult

EXAMPLE_FILES = (".env.example", ".env.sample", ".env.template", "example.env")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=(.*)$")


def parse_env(text: str) -> dict[str, tuple[str, str]]:
    """key -> (value, trailing comment). Commented-out lines are not keys."""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        raw = m.group(2).strip()
        comment = ""
        if raw[:1] and raw[:1] in "\"'" and raw.count(raw[0]) >= 2:
            end = raw.index(raw[0], 1)
            value, rest = raw[1:end], raw[end + 1:]
            comment = rest.split("#", 1)[1] if "#" in rest else ""
        elif raw.startswith("#"):
            value, comment = "", raw[1:]
        else:
            value, _, comment = raw.partition(" #")
            value = value.strip()
        out[m.group(1)] = (value, comment.strip())
    return out


def load_env(repo: Path, names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Merged values of the real env files that exist (later files win), and their names."""
    merged: dict[str, str] = {}
    used: list[str] = []
    for name in names:
        p = repo / name
        if p.is_file():
            used.append(name)
            merged.update({k: v for k, (v, _) in parse_env(p.read_text(encoding="utf-8", errors="replace")).items()})
    return merged, used


def origin(url: str) -> str | None:
    """scheme://host[:port], lowercase, no path; None when it has no host."""
    url = url.strip().strip("'\"")
    if "://" not in url:
        return None
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme.lower()}://{host.lower()}" + (f":{parts.port}" if parts.port else "")


def split_list(value: str) -> list[str]:
    """A comma/space separated list or a JSON array of URLs."""
    value = value.strip()
    if value.startswith("["):
        try:
            data = json.loads(value)
            return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    return [x for x in re.split(r"[,\s;]+", value.strip("[]")) if x]


def host_of(url: str) -> str | None:
    o = origin(url)
    return urlsplit(o).hostname if o else None


def find_key(env: dict[str, str], keys: list[str]) -> tuple[str, str] | None:
    return next(((k, env[k]) for k in keys if env.get(k, "").strip()), None)


def run(ctx: CheckContext) -> CheckResult:
    pc = ctx.cfg.preflight
    res = CheckResult()
    if not pc.configured:
        res.gaps.append("[preflight] repos is not set in baton.toml: nothing to check")
        return res
    for repo in pc.repos:
        check_repo_keys(repo, pc.env_files, res)
    panels = {o: raw for raw in pc.panel_origins if (o := origin(raw))}
    for raw in pc.panel_origins:
        if origin(raw) is None:
            res.gaps.append(f"panel origin '{raw}' is not a URL with a scheme and host")
    cors_origins: list[str] = []
    redirect_origins: list[str] = []
    if pc.panel_origins:
        if pc.backend is None:
            res.gaps.append("[preflight] backend is not set, so CORS and the auth redirect allow-list "
                            "cannot be checked")
        else:
            cors_origins = check_origin_list(pc.backend, pc, pc.cors_keys, panels, "CORS", res)
            if pc.redirect_keys:
                redirect_origins = check_origin_list(pc.backend, pc, pc.redirect_keys, panels,
                                                     "auth redirect allow-list", res)
            else:
                res.skipped.append("auth redirect allow-list (preflight.redirect_keys is empty)")
    else:
        res.skipped.append("CORS and auth redirect allow-list (preflight.panel_origins is empty)")
    check_hosts(pc, panels, cors_origins, redirect_origins, res)
    return res


def _required_keys(example: Path) -> dict[str, str]:
    """The keys an example env file asks for (those not marked optional in their comment)."""
    return {k: c for k, (_, c) in parse_env(example.read_text(encoding="utf-8", errors="replace")).items()
            if "optional" not in c.lower()}


def check_repo_keys(repo: Path, env_files: list[str], res: CheckResult) -> None:
    name = repo.name
    if not repo.is_dir():
        res.gaps.append(f"{name}: folder {repo} does not exist")
        return
    example = next((repo / f for f in EXAMPLE_FILES if (repo / f).is_file()), None)
    if example is None:
        res.skipped.append(f"{name}: no {EXAMPLE_FILES[0]}, so its required keys are unknown")
        return
    required = _required_keys(example)
    env, used = load_env(repo, env_files)
    if not used:
        res.gaps.append(f"{name}: none of {', '.join(env_files)} exists, so all {len(required)} keys "
                        f"of {example.name} are unset")
        return
    missing = sorted(k for k in required if k not in env)
    empty = sorted(k for k in required if k in env and not env[k].strip())
    if missing:
        res.gaps.append(f"{name}: {len(missing)} key(s) of {example.name} not set in {', '.join(used)}: "
                        + ", ".join(missing))
    if empty:
        res.gaps.append(f"{name}: key(s) set but empty in {', '.join(used)}: " + ", ".join(empty))
    if not missing and not empty:
        res.checked.append(f"{name}: all {len(required)} keys of {example.name} are set in {', '.join(used)}")


def check_origin_list(backend: Path, pc, keys: list[str], panels: dict[str, str], what: str,
                      res: CheckResult) -> list[str]:
    """The origins listed under the first of `keys` that the backend's env sets."""
    env, used = load_env(backend, pc.env_files)
    found = find_key(env, keys)
    if not found:
        res.gaps.append(f"{backend.name}: none of {', '.join(keys)} is set in "
                        f"{', '.join(used) or ', '.join(pc.env_files)}; cannot confirm the {what} "
                        "includes the panels")
        return []
    key, value = found
    entries = split_list(value)
    origins = [o for e in entries if (o := origin(e))]
    if "*" in entries:
        res.checked.append(f"{backend.name}: {key} allows every origin (*)")
        return origins
    missing = [raw for o, raw in panels.items() if o not in origins]
    if missing:
        res.gaps.append(f"{backend.name}: {key} ({what}) does not include " + ", ".join(missing))
    else:
        res.checked.append(f"{backend.name}: {key} ({what}) includes all {len(panels)} panel origin(s)")
    return origins


def check_hosts(pc, panels: dict[str, str], cors: list[str], redirects: list[str], res: CheckResult) -> None:
    where: dict[str, list[str]] = {}

    def add(host: str | None, label: str) -> None:
        if host in LOOPBACK:
            labels = where.setdefault(host, [])
            if label not in labels:
                labels.append(label)

    if pc.auth_callback:
        add(host_of(pc.auth_callback), "auth_callback")
    for o in panels:
        add(host_of(o), "panel_origins")
    for o in cors:
        add(host_of(o), "CORS list")
    for o in redirects:
        add(host_of(o), "auth redirect allow-list")
    if len(where) > 1:
        res.gaps.append("loopback hosts are mixed: " + "; ".join(
            f"{h} in {', '.join(w)}" for h, w in sorted(where.items()))
            + ". Use one spelling everywhere: host-only cookies set on one are not sent to the other")
    elif where or pc.auth_callback:
        res.checked.append("loopback hosts are consistent"
                           + (f" ({next(iter(where))})" if where else " (none in use)"))
