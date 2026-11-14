"""Which model ids the installed Codex knows, so a typo fails `baton plan`
instead of a task run.

Codex keeps the models it offers in `models_cache.json` in its home folder
(`~/.codex`, or `providers.codex.home`). `providers.codex.known_models` lists
ids by hand and takes precedence. With neither, the ids cannot be checked and
baton says so.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from .config import Config
from .providers import home_dir

ID_KEYS = ("slug", "id", "model", "name")


def _cache_items(home: Path) -> list | None:
    """The entries of Codex's models cache, or None when it cannot be read or has no list."""
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("models") if isinstance(data, dict) else data
    return items if isinstance(items, list) else None


def _model_ids(items: list, keys: tuple | list) -> set[str]:
    found = {str(it[k]) for it in items if isinstance(it, dict) for k in keys if it.get(k)}
    return found | {it for it in items if isinstance(it, str)}


def installed_models(home: Path | None) -> set[str] | None:
    """Model ids in Codex's models cache, or None when there is no readable cache."""
    items = _cache_items(home) if home is not None else None
    if items is None:
        return None
    found = _model_ids(items, ID_KEYS[:1])
    if not found:   # another layout: fall back to the other id keys
        found = _model_ids(items, ID_KEYS)
    return found or None


def known_models(cfg: Config) -> set[str] | None:
    pc = cfg.providers.get("codex")
    if pc is None:
        return None
    if pc.known_models:
        return set(pc.known_models)
    return installed_models(home_dir(pc))


def check(cfg: Config, rungs: set[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the Codex model ids of the given rungs."""
    known = known_models(cfg)
    if known is None:
        return [], ["codex: cannot check model ids (no models_cache.json in the Codex home); "
                    "list them in providers.codex.known_models to have them checked"]
    errors = []
    for rung in sorted(rungs):
        spec = cfg.routing.spec(rung, "codex")
        if spec is None or spec.model in known:
            continue
        near = difflib.get_close_matches(spec.model, sorted(known), n=3)
        errors.append(f"rung {rung}: codex model '{spec.model}' is not known to the installed codex"
                      + (f" (did you mean {', '.join(near)}?)" if near else f" (known: {', '.join(sorted(known))})"))
    return errors, []
