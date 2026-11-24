"""`baton review-fix`: one round of fixes for the blockers of a review, by the provider that did the work.

Rules, all from the operator's spec:
  * only the provider that implemented the task (no fallback to the other one): when it is limited, the
    command waits for the reset like a normal task would (up to `[run] max_sleep_hours`), otherwise it
    exits `blocked` with the reason;
  * needs a valid review that has at least one [blocker]; [should] is optional, [nit] is ignored;
  * exactly one round: `fix_round` is recorded before the agent starts (a crash does not grant a second
    round) and a second run is refused unless `--force`;
  * the fixer commits on the task branch and never pushes; baton appends a `## Review fix` section to
    results/<id>.md that says, per finding, `fixed` or `declined: <reason>`;
  * nothing re-reviews automatically.

The fixer runs through `Runner.execute`, exactly as an implementer attempt does (same settings, hidden
paths, timeouts, stall watchdog and orphan clean-up); only the prompt differs. Its attempt files are named
`attempt-9RA.<provider>.*` (R = the round, A = the attempt) so they never collide with the task's own."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from . import gitutil
from .config import Config
from .limits import classify, read_output
from .ops import OpsError, exclusive, find_task, load_ordered
from .pathcheck import expand
from .providers import home_dir, resolve_bin
from .reviewrun import ReviewBlocked, final_message, goal_and_acceptance, implementer_of, new_runner
from .reviewstate import Finding, ParsedReview, read_review, review_path, reviewable
from .state import FINISHED
from .tasks import Task

REPORT_NAME = "review-fix.md"
ANSWER_RE = re.compile(r"^\s*[-*]?\s*`?([BS]\d+)`?\s*[:\-–—]\s*(fixed|declined|skipped)\b[\s:\-–—]*(.*?)\s*$",
                       re.IGNORECASE)
MAX_TRANSIENT = 3


def report_path(cfg: Config, task_id: str) -> Path:
    return cfg.results_dir / task_id / REPORT_NAME


def numbered(parsed: ParsedReview) -> list[tuple[str, Finding]]:
    """B1, B2 … for the blockers and S1, S2 … for the [should] findings. [nit] is left out."""
    return ([(f"B{i}", f) for i, f in enumerate(parsed.of("blocker"), 1)]
            + [(f"S{i}", f) for i, f in enumerate(parsed.of("should"), 1)])


# ---------------------------------------------------------------- the checks before anything runs


def check_fixable(runner, task: Task, force: bool) -> ParsedReview:
    cfg, ts = runner.cfg, runner.state.task(task.id)
    if not reviewable(task):
        raise OpsError(f"{task.id} is a {task.kind}; only tasks that implement something have reviews")
    if ts.status not in FINISHED:
        raise OpsError(f"{task.id} is {ts.status}; review-fix works on a finished task with a review")
    parsed = read_review(cfg, task.id)
    if parsed is None:
        raise OpsError(f"{task.id} has no review ({review_path(cfg, task.id)} does not exist). "
                       f"Run `baton review --only {task.id}` first.")
    if not parsed.valid or ts.review_verdict in ("invalid", "blocked"):
        raise OpsError(f"the review of {task.id} is not usable ({parsed.problem or ts.review_verdict}); "
                       f"run `baton review --only {task.id}` again")
    if not parsed.of("blocker"):
        raise OpsError(f"the review of {task.id} has no [blocker] findings, so there is nothing to fix "
                       f"(verdict: {parsed.verdict}). [should] and [nit] findings are never fixed on their own.")
    if ts.fix_round >= 1 and not force:
        raise OpsError(f"{task.id} already had a review-fix round (round {ts.fix_round}, {ts.fix_state or 'ended'}). "
                       "Only one round is allowed; --force runs another.")
    return parsed


def fixer_problem(runner, task: Task, impl: str) -> str | None:
    """Why the implementer cannot fix at all (limits are not a problem: they are waited out)."""
    cfg = runner.cfg
    if not impl:
        return ("baton does not know which provider implemented it (marked done by hand?), and the fixer must "
                "be that provider")
    if impl not in cfg.enabled_providers():
        return f"{impl}, which implemented it, is not enabled in baton.toml"
    if cfg.spec(task.resolved_rung, impl) is None:
        return f"{impl} has no model for rung {task.resolved_rung} in [routing.rungs]"
    if not resolve_bin(cfg.providers[impl]):
        return f"{impl}: '{cfg.providers[impl].bin}' not found or not executable"
    return None


def check_repository(task: Task) -> None:
    repo = task.repo
    if not gitutil.is_repo(repo) or not task.branch or not gitutil.branch_exists(repo, task.branch):
        raise OpsError(f"{task.id}: its branch '{task.branch}' does not exist in {repo.name}")
    busy = gitutil.operation_in_progress(repo)
    if busy:
        raise OpsError(f"{repo.name} is in the middle of {busy}; finish or abort it first")
    if gitutil.is_dirty(repo):
        lines = "\n    ".join(gitutil.short_status(repo))
        raise OpsError(f"{repo.name} has uncommitted changes; commit or stash them first (the fixer's commit "
                       f"must hold only its own work):\n    {lines}")


# ---------------------------------------------------------------- the prompt


def build_fix_prompt(cfg: Config, task: Task, prov: str, parsed: ParsedReview, review_text: str,
                     report: Path, note: str) -> str:
    common = expand(cfg.common_prompt.read_text(encoding="utf-8"), cfg.base_dir).strip() if cfg.common_prompt else ""
    items = "\n".join(f"- {key} {f.label()}" for key, f in numbered(parsed))
    commit = ("Commit your work on the current branch with a normal commit." if cfg.provider_kind(prov) == "claude" else
              "Do not run git commit: baton commits for you. Put a line `COMMIT: <conventional commit message>` "
              "at the top of the report.")
    again = f"\n\nNote: {note}\n" if note else ""
    return f"""{common}

# Review fix for task {task.id}

Another AI agent reviewed your finished work on branch `{task.branch}` (already checked out). Fix what \
the review found, once; this is the only fix round.

## The task (for scope)

{goal_and_acceptance(task)}

## Findings to handle

{items}

- `B…` are blockers: address every one. `S…` are optional improvements: do them only when they are small \
and clearly right. `[nit]` findings were left out on purpose: ignore them.
- You may DECLINE a finding when it is wrong or out of scope, but say why.
- Stay inside the task's Goal. No refactors, renames or style changes beyond what a finding needs.
- {commit} Never push, never switch branches, never rewrite history.
- Run the checks the common rules ask for before you finish.

The full review, for context:

{review_text.strip()}
{again}
## When you are done

Write a short report to {report} with one line per finding you were given:

    B1: fixed
    B2: declined: <reason>
    S1: skipped

Then finish with a one-paragraph summary as your final message.
"""


# ---------------------------------------------------------------- running the fixer


def wait_until_available(runner, task: Task, impl: str) -> None:
    """The implementer is the only provider allowed: sleep until its limit resets, or give up if that is
    further away than `[run] max_sleep_hours`."""
    cfg, now = runner.cfg, runner.clock.now()
    model = runner.task_models(task).get(impl)
    until = runner.sched.status[impl].blocked_until(now, model)
    if until is None:
        return
    if until - now > timedelta(hours=cfg.max_sleep_hours):
        raise ReviewBlocked(f"{task.id}: {impl}, which implemented it, is limited until {runner.fmt(until)} "
                            f"(more than {cfg.max_sleep_hours:g}h away). The fix must be done by {impl}; baton "
                            f"does not use the other provider. Run `baton review-fix --only {task.id}` again "
                            "after the reset.")
    runner.log(f"review-fix {task.id}: {impl} is limited until {runner.fmt(until)}; waiting")
    if not runner.clock.sleep_until(until, runner.stop_wanted):
        raise ReviewBlocked(f"{task.id}: stopped while waiting for {impl}'s limit to lift")


def note_limit(runner, task: Task, impl: str, oc) -> None:
    until, how = runner.reset_time(oc, runner.clock.now())
    model = runner.task_models(task).get(impl, "")
    if oc.window == "model" and oc.model and oc.model in model:
        runner.sched.mark_model_limited(impl, oc.model, until)
    else:
        runner.sched.mark_limited(impl, until, oc.window, reset_known=how.startswith("reset time from"))
    runner.save()
    runner.log(f"review-fix {task.id}: {impl} hit its usage limit; available again {runner.fmt(until)} [{how}]")


def run_attempts(runner, task: Task, impl: str, parsed: ParsedReview, review_text: str, rnd: int):
    """Attempts until one ends without a limit or a transient error. Returns the last Attempt."""
    cfg = runner.cfg
    spec = cfg.spec(task.resolved_rung, impl)
    report, note, transient = report_path(cfg, task.id), "", MAX_TRANSIENT
    for k in range(1, cfg.max_limit_cycles + MAX_TRANSIENT + 2):
        wait_until_available(runner, task, impl)
        report.parent.mkdir(parents=True, exist_ok=True)
        prompt = build_fix_prompt(cfg, task, impl, parsed, review_text, report, note)
        att = runner.execute(task, impl, spec, prompt, 900 + rnd * 10 + min(k, 9))
        oc = classify(cfg.provider_kind(impl), read_output(att.out_path), read_output(att.err_path), att.exit_code,
                      runner.clock.now(), runner.tz, home_dir(cfg.providers[impl]), None)
        if oc.kind == "limit":
            note_limit(runner, task, impl, oc)
            runner.commit_leftovers(task, f"{impl} hit its usage limit during the review fix")
            note = f"{impl} hit its usage limit during an earlier attempt; its work so far is committed (see `git log`)."
        elif oc.kind == "transient" and transient > 0:
            transient -= 1
            runner.pause(cfg.transient_wait_s)
        elif oc.kind == "auth":
            raise ReviewBlocked(f"{task.id}: {impl} is not logged in or was refused ({oc.detail})")
        else:
            return att
    raise ReviewBlocked(f"{task.id}: {impl} kept hitting limits or errors; giving up after {k} attempts")


# ---------------------------------------------------------------- what came back


def parse_answers(text: str) -> dict[str, tuple[str, str]]:
    """key -> (fixed|declined|skipped, reason) from the fixer's report lines."""
    out = {}
    for line in text.splitlines():
        m = ANSWER_RE.match(line)
        if m:
            out[m.group(1).upper()] = (m.group(2).lower(), m.group(3))
    return out


def read_answers(runner, task: Task, att, impl: str) -> dict[str, tuple[str, str]]:
    path = report_path(runner.cfg, task.id)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    answers = parse_answers(text)
    return answers or parse_answers(final_message(runner.cfg.provider_kind(impl), read_output(att.out_path)))


def commit_fix(runner, task: Task, att, impl: str) -> str | None:
    """Commit what the fixer left (Codex cannot commit itself). Returns why some work stays uncommitted."""
    if runner.cfg.provider_kind(impl) == "codex":
        runner.commit_codex_attempt(task, report_path(runner.cfg, task.id), att.wall_start, "review fix", None)
    return runner.commit_leftovers(task, "left uncommitted by the review fix")


def line_for(key: str, f: Finding, answers: dict) -> str:
    kind, reason = answers.get(key, ("", ""))
    if kind == "fixed":
        verdict = "fixed"
    elif kind == "declined":
        verdict = f"declined: {reason or 'no reason given'}"
    elif key.startswith("S"):
        verdict = "skipped"
    else:
        verdict = "NOT REPORTED by the fixer"
    return f"- {f.label()} — {verdict}"


def append_section(runner, task: Task, impl: str, rnd: int, parsed: ParsedReview, answers: dict,
                   commits: int, problems: list[str]) -> None:
    cfg = runner.cfg
    path = cfg.results_dir / f"{task.id}.md"
    body = [f"## Review fix (round {rnd}, {impl}, {runner.clock.now().isoformat(timespec='seconds')})", "",
            f"Review: reviews/{task.id}.md (VERDICT: {parsed.verdict}); {commits} new commit(s) on {task.branch}.", ""]
    body += [line_for(key, f, answers) for key, f in numbered(parsed)]
    body += [f"- [nit] findings were not addressed: {len(parsed.of('nit'))} ignored by design"] if parsed.of("nit") else []
    body += ["", *(f"PROBLEM: {p}" for p in problems)] if problems else []
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old.rstrip("\n") + ("\n\n" if old else "") + "\n".join(body) + "\n", encoding="utf-8")


def judge(runner, task: Task, att, parsed: ParsedReview, answers: dict, commits: int, left: str | None) -> list[str]:
    """What is wrong with the outcome, if anything."""
    problems = []
    missing = [k for k, _ in numbered(parsed) if k.startswith("B") and k not in answers]
    if missing:
        problems.append(f"the fixer did not report on {', '.join(missing)}")
    if any(a[0] == "fixed" for a in answers.values()) and commits == 0:
        problems.append("it reports fixes but the branch has no new commit")
    if left:
        problems.append(f"some work could not be committed: {left}")
    if att.timed_out:
        problems.append("the fixer ran out of time")
    return problems


def finish_fix(runner, task: Task, impl: str, att, parsed: ParsedReview, head_before: str | None, rnd: int) -> list[str]:
    left = commit_fix(runner, task, att, impl)
    commits = gitutil.count_commits(task.repo, head_before) if head_before else 0
    answers = read_answers(runner, task, att, impl)
    problems = judge(runner, task, att, parsed, answers, commits, left)
    append_section(runner, task, impl, rnd, parsed, answers, commits, problems)
    return problems


# ---------------------------------------------------------------- the command


def start_round(runner, task: Task) -> int:
    ts = runner.state.task(task.id)
    ts.fix_round += 1
    ts.fix_state = "running"
    runner.save()
    return ts.fix_round


def settle(runner, task: Task, problems: list[str], untouched: bool, before_round: int) -> None:
    ts = runner.state.task(task.id)
    ts.fix_state = "blocked" if problems else "done"
    if untouched and problems:
        ts.fix_round = before_round         # nothing ran: the one round is still available
    runner.save()


def fix_task(runner, task: Task, force: bool) -> str:
    parsed = check_fixable(runner, task, force)
    impl = implementer_of(runner, task)
    why = fixer_problem(runner, task, impl)
    if why:
        raise ReviewBlocked(f"{task.id}: {why}. The fix is never given to the other provider.")
    check_repository(task)
    review_text = review_path(runner.cfg, task.id).read_text(encoding="utf-8", errors="replace")
    before = runner.state.task(task.id).fix_round
    rnd = start_round(runner, task)
    runner.note_initial_branch(task.repo)
    head_before = None
    try:
        gitutil.checkout(task.repo, task.branch, task.base)
        head_before = gitutil.head(task.repo)
        runner.log(f"review-fix {task.id}: {impl} fixes {len(parsed.of('blocker'))} blocker(s) (round {rnd})")
        att = run_attempts(runner, task, impl, parsed, review_text, rnd)
        problems = finish_fix(runner, task, impl, att, parsed, head_before, rnd)
    except (ReviewBlocked, gitutil.GitError) as e:
        untouched = head_before is None or gitutil.head(task.repo) == head_before and not gitutil.is_dirty(task.repo)
        settle(runner, task, [str(e)], untouched, before)
        runner.restore_branches()
        if isinstance(e, ReviewBlocked):
            raise
        raise ReviewBlocked(f"{task.id}: {e}") from e
    settle(runner, task, problems, False, before)
    runner.restore_branches()
    if problems:
        raise ReviewBlocked(f"{task.id}: review-fix finished with problems: " + "; ".join(problems)
                            + f". See the 'Review fix' section of results/{task.id}.md")
    return f"{task.id}: review fix round {rnd} done by {impl}; see the 'Review fix' section of results/{task.id}.md"


def run_fix(cfg: Config, tid: str, force: bool, echo: bool = True) -> str:
    ordered = load_ordered(cfg)
    task = find_task(ordered, tid)
    with exclusive(cfg):
        return fix_task(new_runner(cfg, ordered, echo), task, force)
