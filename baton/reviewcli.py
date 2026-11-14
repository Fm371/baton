"""The command line of `baton review` and `baton review-fix`, and the review block `baton plan` and
`baton status` print. Imports of the heavy modules are inside the functions, so `baton --help` and every
other command start as fast as before.

Exit codes: 0 done (also "nothing to review"); 2 refused or a usage error; 3 blocked (the reviewer or fixer
could not finish, or the reviewer changed the repository); 4 a baton run is active in this folder."""

from __future__ import annotations

import sys


def _with_config(p):
    p.add_argument("-c", "--config", default=None,
                   help="config file (default: baton.toml here or in a parent folder, or $BATON_WORKSPACE)")
    return p


def add_commands(sub):
    """Register `review` and `review-fix` on the subparsers object `sub`, and return it."""
    p = _with_config(sub.add_parser(
        "review", help="independent read-only code review of a finished task by the OTHER provider "
                       "(only when you run it; `baton run` never does)",
        description="Review a done task with the provider that did not implement it. The reviewer reads the "
                    "task's Goal and Acceptance, results/<id>.md and the branch diff, may not change anything, "
                    "and writes reviews/<id>.md whose first line is VERDICT: approve|changes|reject. A review "
                    "never gates other tasks."))
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--only", metavar="ID", help="review this done task (even without review = true)")
    which.add_argument("--pending", action="store_true",
                       help="review every task with review = true that is done and has no review yet")
    p.add_argument("--allow-same", action="store_true",
                   help="let the implementing provider review its own work when the other one cannot "
                        "(or when baton does not know who implemented it)")
    p.set_defaults(func=cmd_review)
    p = _with_config(sub.add_parser(
        "review-fix", help="one round of fixes for a review's [blocker] findings, by the provider that "
                           "implemented the task",
        description="Give the [blocker] findings of reviews/<id>.md to the provider that implemented the "
                    "task (never the other one; a limit is waited out or reported as blocked). It commits on "
                    "the task branch, never pushes, and baton appends a 'Review fix' section to "
                    "results/<id>.md. One round only; --force allows another. Nothing is re-reviewed."))
    p.add_argument("--only", metavar="ID", required=True, help="the task whose review to act on")
    p.add_argument("--force", action="store_true", help="run another round although one already ran")
    p.set_defaults(func=cmd_review_fix)
    return sub


def _guarded(args, work) -> int:
    """Run `work(cfg)` under the common error handling and the run-active check."""
    from . import cli
    from .ops import OpsError
    from .reviewrun import ReviewBlocked
    from .watch import active_message, run_active
    cfg = cli._cfg(args)
    if run_active(cfg):
        print(active_message(cfg), flush=True)
        return 4
    try:
        return work(cfg)
    except ReviewBlocked as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 3
    except OpsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_review(args) -> int:
    from .reviewrun import run_reviews
    return _guarded(args, lambda cfg: run_reviews(cfg, args.only, args.pending, args.allow_same, print))


def cmd_review_fix(args) -> int:
    from .reviewfix import run_fix

    def work(cfg) -> int:
        print(run_fix(cfg, args.only, args.force))
        return 0
    return _guarded(args, work)


# ---------------------------------------------------------------- plan and status


def print_review_block(tasks, state) -> None:
    from .reviewstate import review_report
    for line in review_report(tasks, state):
        print(line)


def plan_block(cfg, tasks) -> None:
    """What `baton plan` adds: the review state of the tasks that have one (reads the state, writes nothing)."""
    from .state import load_state
    print_review_block(tasks, load_state(cfg.state_dir / "run.json"))
