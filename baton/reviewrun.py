"""`baton review`: an independent, read-only code review of a finished task by the other provider.

Never called by the scheduler: only the operator command reaches this module. The review is a file,
reviews/<task id>.md (format and parsing: baton.reviewstate). It gates nothing; no part of the ready-task
logic reads it.

How the reviewer is kept read-only, per provider:
  * Claude: a per-run settings file = the user's settings + deny rules for hidden paths (verifiers/ and
    their results) + `Edit` denies for the repository and for everything in the baton folder except
    reviews/ + `Bash` denies for the usual file-changing and git-changing commands + an allow rule for
    reviews/<id>.md. Rules match by command prefix, so an interpreter (`python3 -c`) can still write:
    that is why the working tree is snapshotted before and after, see below.
  * Codex: `--sandbox read-only`, no network, no extra writable directories; a config that changes the
    sandbox through extra_args is refused. Codex cannot write reviews/ either, so baton writes the file from
    its final message.
  * Either: `git status`, `HEAD`, the refs, the stash and a hash of the uncommitted changes are compared
    before and after the run. If anything differs, the review is discarded and marked `blocked`; baton
    resets nothing (it never runs reset or clean) and says what changed."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path

from . import gitutil
from .agentrun import AgentRun, launch
from .config import Config
from .isolation import deny_rules, hidden_paths, visible_dirs, write_claude_settings
from .limits import classify, read_output
from .ops import OpsError, find_task, load_ordered
from .providers import build_command, home_dir, resolve_bin
from .reviewstate import (INVALID, normalised, parse_review, review_path, reviewable, reviews_dir)
from .tasks import Task

BLOCKED = "blocked"
DIFF_LIMIT = 150_000          # characters of diff put in the prompt; the --stat always lists every file
RESULT_LIMIT = 20_000
BODY_LIMIT = 8_000

UNSAFE_CODEX_ARGS = ("--sandbox", "-s", "--yolo", "--full-auto", "--dangerously-bypass-approvals-and-sandbox",
                     "danger-full-access", "workspace-write")

# Commands a read-only reviewer has no use for. Prefix rules: a best effort, backed by the snapshot check.
BASH_DENIES = [f"Bash({c}:*)" for c in (
    "git add", "git commit", "git checkout", "git switch", "git reset", "git restore", "git clean",
    "git stash", "git rebase", "git merge", "git cherry-pick", "git revert", "git push", "git pull",
    "git fetch", "git rm", "git mv", "git apply", "git am", "git branch", "git tag", "git config",
    "rm", "mv", "cp", "sed -i", "tee", "touch", "mkdir", "chmod", "ln")]


class ReviewBlocked(OpsError):
    """The review could not be completed or must not be trusted (exit code 3)."""


# ---------------------------------------------------------------- who reviews


def implementer_of(runner, task: Task) -> str:
    """The provider that implemented the task, recorded when it finished (see state.record_implementers)."""
    ts = runner.state.task(task.id)
    return ts.implemented_by or (ts.providers[-1] if ts.providers else "")


def provider_problem(runner, task: Task, prov: str) -> str | None:
    """Why `prov` cannot review this task now, or None."""
    cfg = runner.cfg
    if prov not in cfg.enabled_providers():
        return f"{prov} is not enabled in baton.toml"
    if cfg.routing.spec(task.resolved_rung, prov) is None:
        return f"{prov} has no model for rung {task.resolved_rung} in [routing.rungs]"
    if not resolve_bin(cfg.providers[prov]):
        return f"{prov}: '{cfg.providers[prov].bin}' not found or not executable"
    status = runner.sched.status[prov]
    if status.disabled:
        return f"{prov} is disabled for this run ({status.disabled})"
    until = status.blocked_until(runner.clock.now(), runner.task_models(task).get(prov))
    if until is not None:
        return f"{prov} is limited until {runner.fmt(until)}"
    return None


def choose_reviewer(runner, task: Task, allow_same: bool) -> tuple[str, str]:
    """(reviewer, implementer). The other provider; never the implementer unless `allow_same`."""
    impl = implementer_of(runner, task)
    enabled = runner.cfg.enabled_providers()
    if not impl:
        if not allow_same:
            raise OpsError(f"{task.id}: baton does not know which provider implemented it (it was marked done "
                           "by hand, or finished before baton recorded it). Pass --allow-same to let any "
                           "available provider review it.")
        candidates = enabled
    else:
        others = [p for p in enabled if p != impl]
        candidates = others + ([impl] if allow_same else [])
    if not candidates:
        raise OpsError(f"{task.id}: no provider other than {impl} is enabled, so there is no independent "
                       "reviewer. Enable the other provider, or pass --allow-same to review with "
                       f"{impl} itself.")
    problems = {p: provider_problem(runner, task, p) for p in candidates}
    for p in candidates:
        if problems[p] is None:
            return p, impl
    why = "; ".join(f"{p}: {m}" for p, m in problems.items())
    tail = "" if allow_same else (f" Baton does not fall back to {impl}, the provider that implemented it; "
                                  "wait for the reviewer, or pass --allow-same.") if impl else ""
    raise ReviewBlocked(f"{task.id}: no provider can review it now ({why}).{tail}")


# ---------------------------------------------------------------- what the reviewer reads


def task_diff(cfg: Config, task: Task, start_head: str | None) -> tuple[str, str, str]:
    """(range, stat, diff) of the task branch against its base, the diff cut at DIFF_LIMIT characters."""
    repo, branch = task.repo, task.branch or ""
    if not gitutil.is_repo(repo):
        raise OpsError(f"{task.id}: {repo} is not a git repository")
    if not branch or not gitutil.branch_exists(repo, branch):
        raise OpsError(f"{task.id}: its branch '{branch}' does not exist in {repo.name}; nothing to review")
    base = task.base or cfg.default_base
    if base and gitutil.ref_exists(repo, base):
        rng = f"{base}...{branch}"
    elif start_head:
        rng = f"{start_head}..{branch}"
    else:
        raise OpsError(f"{task.id}: base '{base}' does not exist in {repo.name}, so there is nothing to diff against")
    stat = gitutil.printable(gitutil.git(repo, "diff", "--stat", "--no-ext-diff", rng))
    diff = gitutil.printable(gitutil.git(repo, "diff", "--no-ext-diff", rng))
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + (f"\n\n[diff cut here: {len(diff) - DIFF_LIMIT} more characters. The stat above "
                                    "lists every changed file; read the rest with `git show` on the branch.]\n")
    return rng, stat, diff


def goal_and_acceptance(task: Task) -> str:
    parts = [(t, task.section(t)) for t in ("Goal", "Acceptance")]
    found = [f"### {t}\n\n{text}" for t, text in parts if text]
    if found:
        return "\n\n".join(found)
    return "(the task has no Goal or Acceptance section; its whole text follows)\n\n" + task.body[:BODY_LIMIT]


FORMAT_RULES = """## What to produce

Your final message is the review. Its FIRST line is exactly one of:

    VERDICT: approve
    VERDICT: changes
    VERDICT: reject

Then one finding per line, each starting with a tag, a `path:line`, a dash and the reason:

    [blocker] src/app.py:42 — the total is computed before the discount is applied
    [should]  src/app.py:57 — the same helper is called twice
    [nit]     README.md:3 — typo

- `[blocker]`: wrong behaviour, an acceptance criterion that is not met, data loss, a security hole, or a
  change that breaks the tests. Something that must be fixed before the work counts as done.
- `[should]`: a real improvement that is not a defect.  `[nit]`: taste and style.
- `approve`: no blockers. `changes`: at least one blocker that can be fixed in place. `reject`: the
  approach itself is wrong.
- Report only what you can point at in the diff or in the code. No praise, no summary of the diff, no text
  after the findings. Several lines on one finding are fine if the extra lines do not start with a tag.
"""


def build_review_prompt(cfg: Config, task: Task, implementer: str, reviewer: str, result_text: str,
                        rng: str, stat: str, diff: str, target: Path) -> str:
    write_note = (f"The only file you may write is {target} (put the same text there, and also in your final "
                  "message)." if reviewer == "claude" else "You cannot write files; baton saves your final message.")
    return f"""You are reviewing work that another AI agent ({implementer or 'unknown provider'}) finished. You are \
{reviewer}; the point of this review is an independent second opinion.

You are READ-ONLY. Do not edit, create, delete or move any file in the repository, do not run any git \
command that changes anything (add, commit, checkout, switch, reset, stash, branch, merge, rebase, push), and \
do not start servers or install anything. {write_note} Baton compares the working tree before and after \
you finish; if anything changed, your review is thrown away.

The checkout may be on a different branch than `{task.branch}`. The authoritative change is the diff below. \
To read a file as the task left it, use `git show {task.branch}:<path>`.

# The task: {task.id}

{goal_and_acceptance(task)}

# The implementer's result file

{result_text or '(there is no result file)'}

# The change: git diff {rng}

{stat or '(no changed files)'}

```diff
{diff or '(the branch has no changes against its base)'}
```

{FORMAT_RULES}"""


# ---------------------------------------------------------------- keeping it read-only


def tree_snapshot(repo: Path) -> dict:
    """Everything a read-only run must leave alone: HEAD, branch, refs, stash, and the uncommitted state."""
    top = gitutil.toplevel(repo)
    status = gitutil.git(top, "status", "--porcelain", "-z", "--untracked-files=all")
    tracked = gitutil.git(top, "diff", "HEAD", "--binary", "--no-ext-diff", check=False)
    untracked = []
    for path in gitutil.untracked_files(top):
        try:
            st = os.stat(top / path)
            untracked.append((path, st.st_size, st.st_mtime_ns))
        except OSError:
            untracked.append((path, -1, -1))
    return {
        "head": gitutil.head(top) or "",
        "branch": gitutil.current_branch(top),
        "refs": gitutil.git(top, "for-each-ref", "--format=%(refname) %(objectname)", check=False),
        "stash": gitutil.git(top, "stash", "list", check=False),
        "status": status,
        "tracked_changes": hashlib.sha256(tracked.encode("utf-8", "surrogateescape")).hexdigest(),
        "untracked": untracked,
    }


def snapshot_difference(before: dict, after: dict) -> list[str]:
    return [k for k in before if before[k] != after[k]]


def write_denies(cfg: Config, task: Task) -> list[str]:
    """`Edit` deny rules for the repository and for every entry of the baton folder except reviews/."""
    keep = reviews_dir(cfg).resolve()
    protected = [gitutil.toplevel(task.repo)]
    if cfg.base_dir.is_dir():
        protected += [p.resolve() for p in cfg.base_dir.iterdir() if p.resolve() != keep]
    rules = []
    for p in protected:
        rules += [f"Edit(/{p})", f"Edit(/{p}/**)"]
    return rules


def claude_reviewer_command(runner, task: Task, spec, stem: Path, target: Path) -> tuple[list[str], dict]:
    cfg = runner.cfg
    pc = cfg.providers["claude"]
    if pc.permission_mode in ("bypassPermissions",):
        pc = dataclasses.replace(pc, permission_mode="default")      # deny rules must stay in force
    hidden = hidden_paths(cfg, task, runner.verifiers)
    rules = deny_rules(hidden) + write_denies(cfg, task) + BASH_DENIES
    settings = write_claude_settings(pc.settings, rules, Path(f"{stem}.settings.json"), cfg.base_dir)
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    allow += [f"Edit(/{reviews_dir(cfg).resolve()}/**)"]
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    reviews_dir(cfg).mkdir(parents=True, exist_ok=True)
    add_dirs = visible_dirs([*pc.add_dirs, str(reviews_dir(cfg))], hidden)
    return build_command(pc, spec, cfg.max_turns or None, settings=str(settings), add_dirs=add_dirs)


def codex_reviewer_command(runner, task: Task, spec) -> tuple[list[str], dict]:
    pc = runner.cfg.providers["codex"]
    clash = [a for a in pc.extra_args if a in UNSAFE_CODEX_ARGS]
    if clash:
        raise ReviewBlocked(f"providers.codex.extra_args has {', '.join(clash)}, which changes the sandbox; "
                            "the reviewer must run with --sandbox read-only. Remove it for reviews.")
    pc = dataclasses.replace(pc, sandbox="read-only", add_dirs=[], network=False)
    return build_command(pc, spec, None)


def reviewer_command(runner, task: Task, prov: str, stem: Path, target: Path) -> tuple[list[str], dict]:
    spec = runner.cfg.routing.spec(task.resolved_rung, prov)
    if prov == "claude":
        return claude_reviewer_command(runner, task, spec, stem, target)
    return codex_reviewer_command(runner, task, spec)


# ---------------------------------------------------------------- reading what came back


def final_message(prov: str, out_text: str) -> str:
    """The agent's last message from its JSON event stream ("" if it printed none)."""
    last = ""
    for line in out_text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        item = ev.get("item")
        if prov == "codex":
            if (ev.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                last = item["text"]
        elif ev.get("type") == "result" and isinstance(ev.get("result"), str) and not ev.get("is_error"):
            last = ev["result"]
    return last


def mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def review_text(prov: str, run: AgentRun, target: Path, before_ns: int | None = None) -> str:
    """The review: the file the reviewer wrote (Claude), else its final message. A text that parses wins.
    An older review that was already in reviews/ is never taken for the new one."""
    candidates = []
    written = mtime_ns(target)
    if written is not None and written != before_ns and written / 1e9 >= run.wall_start - 1:
        candidates.append(target.read_text(encoding="utf-8", errors="replace"))
    candidates.append(final_message(prov, read_output(run.out_path)))
    for text in candidates:
        if parse_review(text).valid:
            return text
    return next((c for c in candidates if c.strip()), "")


def archive_previous(cfg: Config, target: Path) -> None:
    """Keep the review a new one replaces under reviews/history/."""
    if not target.is_file():
        return
    hist = reviews_dir(cfg) / "history"
    hist.mkdir(parents=True, exist_ok=True)
    n = 1
    while (hist / f"{target.stem}-{n}.md").exists():
        n += 1
    shutil.copy2(target, hist / f"{target.stem}-{n}.md")


# ---------------------------------------------------------------- one review


def check_limit(runner, task: Task, prov: str, run: AgentRun) -> None:
    """A reviewer that hit its usage limit or an auth error is a blocked review, not a verdict."""
    cfg = runner.cfg
    oc = classify(prov, read_output(run.out_path), read_output(run.err_path), run.code, runner.clock.now(),
                  runner.tz, home_dir(cfg.providers[prov]), None)
    if oc.kind == "limit":
        until, _ = runner.reset_time(oc, runner.clock.now())
        runner.sched.mark_limited(prov, until, oc.window)
        runner.save()
        raise ReviewBlocked(f"{task.id}: {prov} hit its usage limit while reviewing (available again "
                            f"{runner.fmt(until)}); no verdict was recorded")
    if oc.kind == "auth":
        raise ReviewBlocked(f"{task.id}: {prov} is not logged in or was refused ({oc.detail}); no verdict")


def record(runner, task: Task, prov: str, verdict: str) -> None:
    ts = runner.state.task(task.id)
    ts.review_verdict, ts.review_by = verdict, prov
    ts.review_at = runner.clock.now().isoformat()
    runner.save()


def store_blocked(runner, task: Task, prov: str, changed: list[str], before: dict, after: dict) -> None:
    detail = "\n".join(f"- {k}: changed" for k in changed)
    path = reviews_dir(runner.cfg) / f"{task.id}.blocked.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"The {prov} reviewer changed the repository, so its review was discarded.\n\n{detail}\n"
                    f"\nBefore: HEAD {before['head'][:12]} on '{before['branch']}'\n"
                    f"After:  HEAD {after['head'][:12]} on '{after['branch']}'\n"
                    "baton resets and cleans nothing: look at `git status` and `git reflog`.\n", encoding="utf-8")
    record(runner, task, prov, BLOCKED)


def execute_review(runner, task: Task, prov: str, impl: str) -> AgentRun:
    cfg = runner.cfg
    ts = runner.state.task(task.id)
    rng, stat, diff = task_diff(cfg, task, ts.start_head)
    result = cfg.results_dir / f"{task.id}.md"
    result_text = result.read_text(encoding="utf-8", errors="replace")[:RESULT_LIMIT] if result.is_file() else ""
    target = review_path(cfg, task.id)
    n = 1
    while (cfg.results_dir / task.id / f"review-{n:02d}.{prov}.out").exists():
        n += 1
    stem = cfg.results_dir / task.id / f"review-{n:02d}.{prov}"
    prompt = build_review_prompt(cfg, task, impl, prov, result_text, rng, stat, diff, target)
    argv, extra = reviewer_command(runner, task, prov, stem, target)
    timeout = 60 * (task.timeout_min or cfg.task_timeout_min)
    return launch(runner, task, argv, {**os.environ, **extra}, prompt, stem, timeout, task.repo)


def review_task(runner, task: Task, allow_same: bool) -> str:
    """Review one done task. Returns the line for the operator; raises OpsError / ReviewBlocked."""
    ts = runner.state.task(task.id)
    if not reviewable(task):
        raise OpsError(f"{task.id} is a {task.kind}; only tasks that implement something are reviewed")
    if ts.status != "done":
        raise OpsError(f"{task.id} is {ts.status}; only a done task is reviewed")
    prov, impl = choose_reviewer(runner, task, allow_same)
    before = tree_snapshot(task.repo)
    before_ns = mtime_ns(review_path(runner.cfg, task.id))
    runner.log(f"review {task.id}: {prov} reviews the work of {impl or 'an unknown provider'}")
    run = execute_review(runner, task, prov, impl)
    after = tree_snapshot(task.repo)
    changed = snapshot_difference(before, after)
    if changed:
        store_blocked(runner, task, prov, changed, before, after)
        raise ReviewBlocked(f"{task.id}: the {prov} reviewer changed the repository ({', '.join(changed)}); the "
                            f"review was discarded and marked blocked. Details: reviews/{task.id}.blocked.md")
    check_limit(runner, task, prov, run)
    return store_review(runner, task, prov, run, before_ns)


def store_review(runner, task: Task, prov: str, run: AgentRun, before_ns: int | None = None) -> str:
    cfg = runner.cfg
    target = review_path(cfg, task.id)
    text = review_text(prov, run, target, before_ns)
    if not text.strip():
        raise ReviewBlocked(f"{task.id}: the {prov} reviewer produced no review (exit {run.code}); see "
                            f"{run.out_path} and {run.err_path}")
    parsed = parse_review(text)
    archive_previous(cfg, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(normalised(text) if parsed.valid else text, encoding="utf-8")
    record(runner, task, prov, parsed.verdict or INVALID)
    if not parsed.valid:
        runner.log(f"review {task.id}: not a valid review ({parsed.problem}); saved as it was written")
        return f"{task.id}: INVALID review from {prov} ({parsed.problem}); kept in reviews/{task.id}.md"
    counts = ", ".join(f"{len(parsed.of(s))} {s}" for s in ("blocker", "should", "nit"))
    runner.log(f"review {task.id}: {prov} says {parsed.verdict} ({counts})")
    return f"{task.id}: {parsed.verdict} by {prov} ({counts}) -> reviews/{task.id}.md"


# ---------------------------------------------------------------- the command


def pending_tasks(ordered: list[Task], state) -> list[Task]:
    """review = true, done, and no review yet."""
    return [t for t in ordered if reviewable(t) and t.review and state.task(t.id).status == "done"
            and not state.task(t.id).review_verdict]


def new_runner(cfg: Config, ordered: list[Task], echo: bool = True):
    from .runner import Runner      # not at import time: the runner imports this package's modules
    r = Runner(cfg, echo=echo)
    r.ordered = ordered
    r.verifiers = [t for t in ordered if t.is_verifier]
    return r


def run_reviews(cfg: Config, only: str | None, pending: bool, allow_same: bool, emit, echo: bool = True) -> int:
    """Review the task(s) and `emit` one line per review. Returns the exit code: 0, or 2 when some tasks of a
    --pending run were skipped. Raises OpsError (refused) or ReviewBlocked (a review could not be completed;
    the rest of a --pending run is not attempted)."""
    from .ops import exclusive
    ordered = load_ordered(cfg)
    with exclusive(cfg):
        r = new_runner(cfg, ordered, echo)
        todo = [find_task(ordered, only)] if only else pending_tasks(ordered, r.state)
        if not todo:
            emit("nothing to review: no task with review = true is done without a review")
            return 0
        code = 0
        for t in todo:
            try:
                emit(review_task(r, t, allow_same))
            except ReviewBlocked:
                raise
            except OpsError as e:
                if only:
                    raise
                emit(f"{t.id}: skipped, {e}")
                code = 2
        return code
