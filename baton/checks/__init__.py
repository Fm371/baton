"""Built-in checks that baton runs itself (verifier `template = ...`), without
an agent: env-preflight, browser-smoke, real-data-smoke and repo-suite.

Each module has `run(ctx) -> CheckResult`. A check that cannot run reports
itself blocked; it never passes by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Config
from ..tasks import Task


@dataclass
class CheckContext:
    cfg: Config
    task: Task
    out_dir: Path                                    # results/<id>/ for screenshots and logs
    should_stop: Callable[[], bool] = lambda: False

    @property
    def params(self) -> dict:
        return self.task.params


@dataclass
class CheckResult:
    gaps: list[str] = field(default_factory=list)       # what a human must fix
    checked: list[str] = field(default_factory=list)    # what was looked at and fine
    skipped: list[str] = field(default_factory=list)    # what was not looked at, and why
    files: list[Path] = field(default_factory=list)     # screenshots and logs worth opening

    @property
    def status(self) -> str:
        return "blocked" if self.gaps else "done"

    def markdown(self, task: Task, results_dir: Path) -> str:
        out = [f"STATUS: {self.status}", "", f"# {task.template}: {task.id}", ""]
        out += [f"## Gaps ({len(self.gaps)})", ""]
        out += [f"- {g}" for g in self.gaps] or ["None."]
        if self.checked:
            out += ["", "## Checked", ""] + [f"- {c}" for c in self.checked]
        if self.skipped:
            out += ["", "## Not checked", ""] + [f"- {s}" for s in self.skipped]
        if self.files:
            out += ["", "## Files", ""]
            for p in self.files:
                try:
                    rel = p.relative_to(results_dir).as_posix()
                except ValueError:
                    rel = str(p)
                out.append(f"- ![{p.name}]({rel})" if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                           else f"- {rel}")
        return "\n".join(out) + "\n"


def run_template(ctx: CheckContext) -> CheckResult:
    from . import browser, datasmoke, envcheck, reposuite
    from .servers import CheckError
    modules = {"env-preflight": envcheck, "browser-smoke": browser, "real-data-smoke": datasmoke,
               "repo-suite": reposuite}
    try:
        return modules[ctx.task.template].run(ctx)
    except CheckError as e:
        return CheckResult(gaps=[str(e)])
    except Exception as e:   # a crashing check must block, not pass
        return CheckResult(gaps=[f"{ctx.task.template} crashed: {type(e).__name__}: {e}"])
