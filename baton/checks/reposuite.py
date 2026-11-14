"""repo-suite: run a repository's full test suite once, after the last task of
a feature that touched it, and report only the failures that are new.

Inside a task the agent runs targeted tests only; the full suite is slow and
usually has failures that were there before the feature. This runs it once and
compares what fails with a baseline file (one failing test name per line, `#`
comments), so a human sees what this feature broke and nothing else.

    [repos."../api"]                        # in baton.toml
    suite_cmd = "pytest -q -rf"             # a shell command string, or a list of arguments
    suite_baseline = "baselines/api.txt"    # known failing tests, one per line (an empty file is fine)
    suite_timeout_min = 60
    # suite_failure_regex = "^FAILED (\\S+)"   # one group: the failing test; default: pytest, unittest, go, jest

`baton plan` adds one after the last task of each `feature` for every repository
that has a `suite_cmd` (`[repos."..."]`); a verifier file with `template =
"repo-suite"` and `repo = "../api"` does the same by hand, and `params` may
override `cmd`, `baseline` and `timeout_min`.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path

from .. import gitutil
from ..process import end_group
from . import CheckContext, CheckResult
from .servers import CheckError

# A group around the failing test's name. Tried per line, in order; the first that matches wins.
DEFAULT_FAILURES = [
    re.compile(r"^(?:FAILED|ERROR)\s+(?!\()(\S+?)(?:\s+-\s.*)?$"),   # pytest -rf / -rfE (not unittest's "FAILED (failures=1)")
    re.compile(r"^(?:FAIL|ERROR):\s+(.+?)\s*$"),                    # unittest
    re.compile(r"^--- FAIL:\s+(\S+)"),                              # go test
    re.compile(r"^\s*FAIL\s+(\S+\.(?:[jt]sx?|mjs|cjs)\b.*?)\s*$"),  # jest / vitest file
    re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d+\s*ms\))?\s*$"),   # jest test line
]
TAIL_LINES = 8


def read_baseline(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CheckError(f"baseline file {path} cannot be read: {e}") from e
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def failing_tests(output: str, custom: re.Pattern | None = None) -> list[str]:
    """The names of failing tests found in a suite's output, in order, without repeats."""
    found: list[str] = []
    for line in output.splitlines():
        for rx in ([custom] if custom else DEFAULT_FAILURES):
            m = rx.search(line) if custom else rx.match(line)
            if m:
                name = m.group(1).strip()
                if name and name not in found:
                    found.append(name)
                break
    return found


class SuiteStopped(Exception):
    pass


def run_suite(cmd, repo: Path, timeout: float, should_stop) -> tuple[int, str, str]:
    """Run the suite in its own process group, so a timeout or a stop ends the shell *and* the test runner
    below it, and look at `should_stop` every fraction of a second: Ctrl+C does not wait out the timeout."""
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        proc = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=repo, stdin=subprocess.DEVNULL,
                                stdout=fout, stderr=ferr, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    code = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if should_stop():
                    raise SuiteStopped()
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            end_group(proc.pid, 3.0)        # whatever the suite left running
            if proc.poll() is None:
                proc.wait()
        fout.seek(0)
        ferr.seek(0)
        return code, fout.read().decode(errors="replace"), ferr.read().decode(errors="replace")


def setting(ctx: CheckContext, key: str, repo_value=None, default=None):
    return ctx.params.get(key, repo_value if repo_value is not None else default)


def run(ctx: CheckContext) -> CheckResult:
    """Run the suite and leave the repository as it was found: on the same branch, and without the
    files (coverage.xml, junit.xml, caches) the suite created. Those would otherwise make the repository
    dirty, so the next task there could not switch branch, or get committed onto the wrong branch."""
    repo = ctx.task.repo
    if not gitutil.is_repo(repo):
        return _run(ctx)
    branch_before = gitutil.current_branch(repo)
    untracked_before = set(gitutil.untracked_files(repo))
    modified_before = set(gitutil.changed_files(repo)) - untracked_before
    try:
        res = _run(ctx)
    finally:
        notes = _leave_as_found(repo, branch_before, untracked_before, modified_before)
    res.checked += notes
    return res


def _remove_created(top: Path, created: list[str]) -> list[str]:
    """Delete the untracked files the suite made (and the folders it made, once empty)."""
    removed = []
    for p in created:
        try:
            (top / p).unlink()
            removed.append(gitutil.printable(p))
        except OSError:
            pass
        parent = (top / p).parent
        while parent != top and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()          # a folder the suite made, now empty
            parent = parent.parent
    return removed


def _restore_branch(repo: Path, branch: str) -> str | None:
    """Put the repository back on the branch it was on; a note about what happened, or None."""
    here = gitutil.current_branch(repo)
    if not branch or here == branch:
        return None
    if gitutil.is_dirty(repo):
        return (f"{repo.name} was left on '{here}' because it has uncommitted changes "
                f"(it was on '{branch}' before the suite)")
    try:
        gitutil.git(repo, "checkout", "-q", branch)
        return f"{repo.name} put back on '{branch}'"
    except gitutil.GitError as e:
        return f"could not put {repo.name} back on '{branch}': {e}"


def _leave_as_found(repo: Path, branch: str, untracked_before: set[str], modified_before: set[str]) -> list[str]:
    notes = []
    top = gitutil.toplevel(repo)        # the changed paths are relative to the repository's top folder
    changed = set(gitutil.changed_files(repo))
    created = sorted(set(gitutil.untracked_files(repo)) - untracked_before)
    removed = _remove_created(top, created)
    if removed:
        notes.append(f"the suite created {len(removed)} untracked file(s) in {repo.name}; baton removed them: "
                     f"{', '.join(removed[:8])}{' …' if len(removed) > 8 else ''}")
    touched = sorted(p for p in changed - set(created) - modified_before if p not in untracked_before)
    if touched:
        notes.append(f"the suite changed tracked file(s) in {repo.name} (left as they are): "
                     f"{', '.join(gitutil.printable(p) for p in touched[:8])}{' …' if len(touched) > 8 else ''}")
    back = _restore_branch(repo, branch)
    return notes + ([back] if back else [])


def _suite_settings(ctx: CheckContext):
    """(cmd, baseline_path, baseline, timeout seconds, custom failure regex) for this suite run."""
    cfg = ctx.cfg
    repo = ctx.task.repo
    rc = cfg.repos.get(repo)
    cmd = setting(ctx, "cmd", rc.suite_cmd if rc else None)
    if not cmd:
        raise CheckError(f"no suite_cmd for {repo.name}: set [repos.\"...\"] suite_cmd in baton.toml "
                         "or `cmd` in the verifier's params")
    if not gitutil.is_repo(repo):
        raise CheckError(f"{repo} is not a git repository")
    baseline_path = (cfg.base_dir / ctx.params["baseline"]) if ctx.params.get("baseline") else (
        rc.suite_baseline if rc else None)
    baseline = read_baseline(baseline_path)
    timeout = 60 * float(setting(ctx, "timeout_min", rc.suite_timeout_min if rc else None, 60))
    return cmd, baseline_path, baseline, timeout, rc.suite_failure_regex if rc else None


def _check_out_branch(ctx: CheckContext, res: CheckResult) -> None:
    """The suite runs on the task's branch when it has one and the repository can be moved there."""
    repo = ctx.task.repo
    branch = ctx.params.get("branch")
    if not branch or gitutil.current_branch(repo) == branch:
        return
    if gitutil.is_dirty(repo):
        res.skipped.append(f"{repo.name} has uncommitted changes, so the suite ran on "
                           f"'{gitutil.current_branch(repo)}' and not on '{branch}'")
        return
    try:
        gitutil.checkout(repo, branch, ctx.params.get("base"))
        res.checked.append(f"checked out {repo.name}:{branch}")
    except gitutil.GitError as e:
        raise CheckError(f"cannot check out {branch} to run the suite: {e}") from e


def _execute_suite(ctx: CheckContext, cmd, shown: str, timeout: float, res: CheckResult) -> tuple[int, str]:
    """Run the suite and keep its output in suite.log; (exit code, output)."""
    repo = ctx.task.repo
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    log = ctx.out_dir / "suite.log"
    try:
        returncode, stdout, stderr = run_suite(cmd, repo, timeout, ctx.should_stop)
    except SuiteStopped:
        raise CheckError(f"the suite `{shown}` was stopped before it finished") from None
    except subprocess.TimeoutExpired:
        raise CheckError(f"the suite `{shown}` did not finish in {timeout / 60:g} minutes") from None
    except OSError as e:
        raise CheckError(f"the suite `{shown}` could not start: {e}") from e
    output = stdout + ("\n" if stdout and stderr else "") + stderr
    log.write_text(output, encoding="utf-8")
    res.files.append(log)
    return returncode, output


def _judge(res: CheckResult, repo: Path, shown: str, returncode: int, output: str, baseline: set[str],
           baseline_path: Path | None, custom) -> None:
    """Turn the suite's outcome into gaps (new failures) and notes (what passed or is known)."""
    if returncode == 0:
        res.checked.append(f"`{shown}` in {repo.name}: all passed")
        if baseline:
            res.checked.append(f"{len(baseline)} baseline failure(s) now pass; remove them from "
                               f"{baseline_path.name}: {', '.join(sorted(baseline)[:10])}")
        return
    failures = failing_tests(output, custom)
    if not failures:
        tail = " | ".join(output.strip().splitlines()[-TAIL_LINES:])
        res.gaps.append(f"`{shown}` in {repo.name} exited {returncode}, but no failing test names were found "
                        "in its output, so new failures cannot be told from known ones (use a runner flag that "
                        f"lists them, or set suite_failure_regex). Last lines: {tail}")
        return
    new = [f for f in failures if f not in baseline]
    known = [f for f in failures if f in baseline]
    res.gaps += [f"new failure in {repo.name}: {f}" for f in new[:50]]
    if len(new) > 50:
        res.gaps.append(f"... and {len(new) - 50} more new failure(s), see suite.log")
    if known:
        res.checked.append(f"{len(known)} failure(s) in {repo.name} are in the baseline and were not reported")
        res.skipped.append(f"{len(known)} known failing test(s) in {repo.name} (baseline), not judged by this feature")
    if not new:
        res.checked.append(f"`{shown}` in {repo.name}: exit {returncode}, every failure is in the baseline")


def _run(ctx: CheckContext) -> CheckResult:
    repo = ctx.task.repo
    cmd, baseline_path, baseline, timeout, custom = _suite_settings(ctx)
    res = CheckResult()
    _check_out_branch(ctx, res)
    shown = cmd if isinstance(cmd, str) else " ".join(cmd)
    returncode, output = _execute_suite(ctx, cmd, shown, timeout, res)
    _judge(res, repo, shown, returncode, output, baseline, baseline_path, custom)
    return res
