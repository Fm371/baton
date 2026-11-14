"""Running one agent process outside the scheduler: the read-only reviewer of `baton review`.

`ProcessMixin.execute` builds the command for a task that may write its repository. A reviewer needs a
different command (read-only settings), so this module builds its own and reuses the parts of the Runner
that do not depend on that: the child-process registry, the orphan clean-up record in run.json, and the
SIGTERM/SIGKILL helper. A reviewer that is killed with the terminal is therefore ended by the next run the
same way an implementer is."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentRun:
    code: int | None
    out_path: Path
    err_path: Path
    timed_out: bool
    seconds: float
    wall_start: float


def launch(runner, task, argv: list[str], env: dict, prompt: str, stem: Path, timeout_s: float,
           cwd: Path) -> AgentRun:
    """Start `argv` in its own process group with `prompt` on stdin, wait for it (at most `timeout_s`), and
    end whatever it leaves running. Output goes to <stem>.out / <stem>.err."""
    stem.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(f"{stem}.prompt.md")
    prompt_path.write_text(prompt, encoding="utf-8")
    out_path, err_path = Path(f"{stem}.out"), Path(f"{stem}.err")
    wall, mono = time.time(), time.monotonic()
    timed_out = False
    with prompt_path.open("rb") as fin, out_path.open("wb") as fout, err_path.open("wb") as ferr:
        try:
            child = subprocess.Popen(argv, cwd=cwd, stdin=fin, stdout=fout, stderr=ferr, env=env,
                                     start_new_session=True)
        except OSError as e:
            ferr.write(f"baton: could not start {argv[0]}: {e}\n".encode())
            return AgentRun(127, out_path, err_path, False, 0.0, wall)
        runner._child = child
        runner.register_agent(task, child)
        try:
            code = child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out, code = True, runner._kill(child)
        except BaseException:               # Ctrl+C: do not leave the agent running
            runner._kill(child)
            raise
        finally:
            runner._child = None
            runner.forget_agent(task, child.pid)
    return AgentRun(code, out_path, err_path, timed_out, time.monotonic() - mono, wall)
