"""Human checkpoints (`kind = "approve"`).

baton writes what needs a decision to results/<id>.md and holds every task
that depends on the checkpoint. `baton approve <id>` and `baton reject <id>
--note ...` leave a decision file under state/decisions/; the runner reads it
at the start of each loop, so a decision also reaches a run that is still
busy with other tasks. The run state itself is only ever written by the
runner.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .state import atomic_write
from .tasks import Task, ancestors

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUMMARY_LINES = 40


def decisions_dir(cfg: Config) -> Path:
    return cfg.state_dir / "decisions"


def write_decision(cfg: Config, tid: str, decision: str, note: str = "") -> Path:
    path = decisions_dir(cfg) / f"{tid}.json"
    atomic_write(path, json.dumps({
        "task": tid, "decision": decision, "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, ensure_ascii=False) + "\n")
    return path


def read_decision(cfg: Config, tid: str) -> dict | None:
    path = decisions_dir(cfg) / f"{tid}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return d if d.get("decision") in ("approve", "reject") else None


def retire_decision(cfg: Config, tid: str) -> None:
    """Keep the decision for the record, but never apply it twice."""
    path = decisions_dir(cfg) / f"{tid}.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    try:
        path.rename(path.with_name(f"{tid}.{stamp}.applied.json"))
    except OSError:
        pass


def _result_excerpt(cfg: Config, tid: str) -> str:
    try:
        lines = (cfg.results_dir / f"{tid}.md").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no result file)"
    lines = [x for x in lines if not x.startswith("STATUS:")]
    text = "\n".join(lines[:SUMMARY_LINES]).strip()
    more = "\n(...)" if len(lines) > SUMMARY_LINES else ""
    return (text or "(empty result file)") + more


def screenshots(cfg: Config, tids: list[str]) -> list[Path]:
    found: list[Path] = []
    for tid in tids:
        folder = cfg.results_dir / tid
        if folder.is_dir():
            found += sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXT)
    return found


def request_text(cfg: Config, task: Task, by_id: dict[str, Task], state_status: dict[str, str]) -> str:
    """What the human needs to decide: the open questions (the task body), the
    journey each task before it set out to build, what it reports having
    built, and the screenshots it saved."""
    deps = [by_id[d] for d in task.depends_on if d in by_id]
    upstream = [t.id for t in ancestors(task, by_id)]
    out = ["STATUS: awaiting-approval", "", f"# Approval needed: {task.id}", "",
           "## Open questions", "", task.body.strip(), ""]
    out += ["## Flow summary", ""]
    if not deps:
        out += ["(this checkpoint depends on no task)", ""]
    for d in deps:
        out += [f"### {d.id} ({state_status.get(d.id, 'pending')})", ""]
        journey = d.section("User journey")
        if journey:
            out += ["Intended journey:", "", journey, ""]
        reuse = d.section("Reuse")
        if reuse:
            out += ["Reuse:", "", reuse, ""]
        out += ["Reported by the task:", "", _result_excerpt(cfg, d.id), ""]
    shots = screenshots(cfg, upstream)
    out += ["## Screenshots", ""]
    if shots:
        out += [f"- ![{p.name}]({p.relative_to(cfg.results_dir).as_posix()})" for p in shots]
    else:
        out += ["(none saved under results/<task>/)"]
    out += ["", "## Decide", "",
            f"- `baton approve {task.id}` releases the tasks that depend on it.",
            f"- `baton reject {task.id} --note \"what is wrong\"` marks them blocked with that note.", ""]
    return "\n".join(out)
