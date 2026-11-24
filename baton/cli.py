"""Command line: baton init | run | plan | status | stop | watch | approve | reject | mark | retry | resume | providers | review | review-fix | classify."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .clock import local_zone
from .config import ConfigError, load_config
from .limits import classify
from .reviewcli import add_commands as add_review_commands
from .reviewcli import plan_block, print_review_block


def cmd_init(args) -> int:
    from .initcmd import NEXT_STEPS, InitError, init_workspace
    dest = Path(args.dir).expanduser()
    try:
        written = init_workspace(dest, force=args.force)
    except InitError as e:
        print(f"init error: {e}", file=sys.stderr)
        return 2
    print(NEXT_STEPS.format(dest=dest, n=len(written)), end="")
    return 0


def find_config(given: str | None) -> Path:
    """Which baton.toml: the one named with -c; else $BATON_WORKSPACE (a folder or the file); else the
    nearest one in this folder or above it, so `baton status` works from a subfolder."""
    if given:
        return Path(given)
    env = os.environ.get("BATON_WORKSPACE")
    if env:
        p = Path(env).expanduser()
        return p / "baton.toml" if p.is_dir() else p
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "baton.toml").is_file():
            return d / "baton.toml"
    return here / "baton.toml"


def _cfg(args):
    path = find_config(args.config)
    if not path.is_file():
        print(f"config error: {path} not found. Run baton in your workspace folder (the one that holds "
              "baton.toml, for example ~/work/task-auto), or pass -c PATH, or set BATON_WORKSPACE.",
              file=sys.stderr)
        sys.exit(2)
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_plan(args) -> int:
    from .preflight import plan_text, preflight
    cfg = _cfg(args)
    errors, warnings, tasks = preflight(cfg, args.provider, create_dirs=False)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"PREFLIGHT FAILED: {e}")
    if tasks:
        print(plan_text(cfg, tasks, args.provider))
        plan_block(cfg, tasks)
    if errors:
        return 1
    print("\npreflight passed; nothing was run and nothing was created")
    return 0


def cmd_run(args) -> int:
    from .preflight import preflight
    from .runner import Runner
    cfg = _cfg(args)
    from .watch import active_message, run_active
    if run_active(cfg):          # before preflight, which probes CLIs and creates folders
        print(active_message(cfg), flush=True)
        return 4
    errors, warnings, tasks = preflight(cfg, args.provider)
    for w in warnings:
        print(f"warning: {w}")
    if errors:
        for e in errors:
            print(f"PREFLIGHT FAILED: {e}")
        return 1
    if args.follow and args.only:
        print("--follow cannot be combined with --only", file=sys.stderr)
        return 2
    return Runner(cfg, only=args.only, force=args.force, provider=args.provider, follow=args.follow).run(tasks)


def cmd_watch(args) -> int:
    from .watch import watch
    cfg = _cfg(args)
    try:
        return watch(cfg, sys.stdout, follow=not args.no_follow, lines=args.lines, raw=args.raw, lane=args.lane)
    except KeyboardInterrupt:
        return 0


def cmd_stop(args) -> int:
    """Ask the run in this folder to finish its current task and end."""
    from .watch import run_active
    cfg = _cfg(args)
    if not run_active(cfg):          # creates nothing when there is no run
        print("no baton run is active in this folder")
        return 1
    (cfg.state_dir / "stop").write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    print("stop requested: the run finishes the task in progress (or leaves a wait for a reset) and then ends; "
          "baton run continues from there")
    return 0


def _summary_for_status(cfg, active: bool) -> tuple[str, list]:
    """(text, ordered tasks) for `baton status`. Rebuilt from state/run.json, so a state edited by hand
    or by `baton mark` shows at once; while a run is active and its SUMMARY.md is as new as the
    state, that file is printed, because it also says what the running task is doing."""
    summary = cfg.results_dir / "SUMMARY.md"
    state_file = cfg.state_dir / "run.json"
    ordered: list = []
    try:
        from .autocheck import schedule_preflight
        from .runner import Runner
        from .tasks import load_tasks, order_tasks
        ordered = order_tasks(schedule_preflight(cfg, load_tasks(cfg)))
        fresh_file = (active and summary.is_file() and state_file.is_file()
                      and summary.stat().st_mtime >= state_file.stat().st_mtime)
        if fresh_file:
            return summary.read_text(encoding="utf-8"), ordered
        if not state_file.is_file() and not summary.is_file():
            return "no run yet", ordered
        r = Runner(cfg, echo=False)
        r.ordered = ordered
        return r.summary_text(ordered, ended=not active), ordered
    except Exception as e:          # a task file that does not parse must not hide the last known summary
        note = f"(could not rebuild the summary: {str(e).splitlines()[0] if str(e) else type(e).__name__})\n\n"
        if summary.is_file():
            return note + summary.read_text(encoding="utf-8"), ordered
        return "no run yet" if not state_file.is_file() else note, ordered


def cmd_status(args) -> int:
    from . import ops
    from .state import load_state
    from .watch import run_active
    cfg = _cfg(args)
    active = run_active(cfg)
    text, ordered = _summary_for_status(cfg, active)
    print(text)
    state = load_state(cfg.state_dir / "run.json")
    running = state.running if active else {}
    if running:
        print("running now:")
        for tid, v in running.items():
            print(f"  [{v.get('lane', '?')}] {tid}"
                  + (f" on {v['provider']}" if v.get("provider") else "")
                  + (f", attempt {v['attempt']}" if v.get("attempt") else ""))
    from .watch import activity_lines
    for line in activity_lines(cfg, active):
        print(line)
    print_review_block(ordered, state)
    todo = ops.hints(ordered, state, active) if ordered else []
    if todo:
        print("what to do next:")
        for h in todo:
            print("  " + h)
    log = cfg.logs_dir / "run.log"
    if log.is_file():
        print("last log lines:")
        for line in log.read_text(encoding="utf-8").splitlines()[-args.lines:]:
            print("  " + line)
    return 0


def cmd_mark(args) -> int:
    from . import ops
    cfg = _cfg(args)
    try:
        print(ops.mark(cfg, args.id, args.status, args.note or "", args.result))
    except ops.OpsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_retry(args) -> int:
    from . import ops
    cfg = _cfg(args)
    if args.provider and args.provider not in cfg.enabled_providers():
        print(f"error: --provider {args.provider}: that provider is not enabled", file=sys.stderr)
        return 2
    try:
        before = ops.prepare_retry(cfg, args.id)
    except ops.OpsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if before is None:
        print(f"a run is active: the retry of {args.id} is queued and applied between tasks; "
              "that run picks the task up again by itself")
        return 0
    print(f"{args.id} cleared (it was {before}); running just this task now")
    return cmd_run(argparse.Namespace(config=args.config, only=args.id, force=args.force,
                                      provider=args.provider, follow=False))


def cmd_resume(args) -> int:
    from . import ops
    from .runner import Runner
    from .watch import lock_info, run_active
    cfg = _cfg(args)
    from .state import load_state
    before = load_state(cfg.state_dir / "run.json")
    try:
        if run_active(cfg):
            if (lock_info(cfg) or {}).get("phase") == "cleanup":
                print("the owner is cleaning up branches; waiting for its lock release", flush=True)
            else:
                print(ops.request_resume(cfg, args.id), flush=True)
                if (args.id not in before.agents and before.task(args.id).status in ("running", "waiting")
                        and (lock_info(cfg) or {}).get("phase") != "cleanup"):
                    return 0
            if ops.wait_resume(cfg, args.id, before.task(args.id).attempts):
                return 0
            print("the owner released its lock; continuing through normal crash/WIP recovery", flush=True)
        ordered = ops.load_ordered(cfg)
        ops.find_task(ordered, args.id)
        return Runner(cfg, only=args.id, resume=True, resume_attempt=before.task(args.id).attempts).run(ordered)
    except ops.OpsError as e:
        print(f"resume refused: {e}", file=sys.stderr)
        return 2


def cmd_providers(args) -> int:
    from . import ops
    cfg = _cfg(args)
    try:
        print(ops.providers_reset(cfg, args.name))
    except ops.OpsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _open_dependencies(cfg, task, tasks) -> list[str]:
    """The dependencies of a checkpoint that are not finished (a verifier or checkpoint must be `done`)."""
    from .state import FINISHED, load_state
    state, by_id = load_state(cfg.state_dir / "run.json"), {t.id: t for t in tasks}
    out = []
    for d in task.depends_on:
        dep, status = by_id.get(d), state.task(d).status
        if dep is not None and not (status == "done" if dep.is_verifier or dep.is_approval else status in FINISHED):
            out.append(f"{d} ({status})")
    return out


def _decide(args, decision: str) -> int:
    from . import approval
    from .autocheck import schedule_preflight
    from .state import load_state
    from .tasks import TaskError, load_tasks
    cfg = _cfg(args)
    try:
        tasks = schedule_preflight(cfg, load_tasks(cfg))
    except TaskError as e:
        print(f"task error: {e}", file=sys.stderr)
        return 2
    task = next((t for t in tasks if t.id == args.id), None)
    if task is None or not task.is_approval:
        print(f"'{args.id}' is not an approval checkpoint (kind = \"approve\")", file=sys.stderr)
        return 2
    from .watch import run_active
    ts = load_state(cfg.state_dir / "run.json").task(task.id)
    status = ts.status
    if status not in ("awaiting", "rejected"):
        if status == "done":
            print(f"{task.id} is already decided ({ts.note or 'done'}); nothing to do", file=sys.stderr)
        else:
            print(f"{task.id} is {status}: baton has not asked for this decision yet. It writes its request "
                  f"(results/{task.id}.md) when the tasks before it are done; "
                  + ("the running baton will get there" if run_active(cfg) else "run `baton run` first")
                  + ", then decide.", file=sys.stderr)
        return 2
    if decision == "approve":
        open_deps = _open_dependencies(cfg, task, tasks)
        if open_deps:
            print(f"{task.id} cannot be approved yet: it depends on {', '.join(open_deps)}, which "
                  "has not finished. Approve it once they are done.", file=sys.stderr)
            return 2
    approval.write_decision(cfg, task.id, decision, args.note or "")
    if decision == "approve":
        print(f"approved {task.id}; "
              + ("the running baton picks it up by itself and releases the tasks that wait for it"
                 if run_active(cfg) else
                 "run `baton run` to release the tasks that wait for it"))
    else:
        print(f"rejected {task.id}; the tasks that depend on it will be marked blocked")
    return 0


def cmd_approve(args) -> int:
    return _decide(args, "approve")


def cmd_reject(args) -> int:
    if not args.note:
        print("reject needs --note saying what is wrong", file=sys.stderr)
        return 2
    return _decide(args, "reject")


def cmd_classify(args) -> int:
    """Classify a saved attempt output, to test limit detection on real samples."""
    try:
        out = Path(args.stdout).read_text(encoding="utf-8", errors="replace")
        err = Path(args.stderr).read_text(encoding="utf-8", errors="replace") if args.stderr else ""
    except OSError as e:
        print(f"cannot read {e.filename or 'the output file'}: {e.strerror or e}", file=sys.stderr)
        return 2
    try:
        tz = ZoneInfo(args.timezone) if args.timezone else local_zone()
    except (ZoneInfoNotFoundError, ValueError, OSError):
        print(f"unknown timezone '{args.timezone}' (use an IANA name such as Europe/Berlin)", file=sys.stderr)
        return 2
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError:
        print(f"--now '{args.now}' is not an ISO date-time such as 2026-10-24T18:00:00", file=sys.stderr)
        return 2
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    oc = classify(args.provider, out, err, args.exit_code, now, tz)
    print(f"kind:     {oc.kind}")
    print(f"window:   {oc.window or '-'}")
    print(f"reset_at: {oc.reset_at.astimezone(tz).isoformat() if oc.reset_at else '-'}")
    print(f"source:   {oc.source}")
    print(f"session:  {oc.session_id or '-'}")
    print(f"detail:   {oc.detail or '-'}")
    return 0


def _with_config(p):
    p.add_argument("-c", "--config", default=None,
                   help="config file (default: baton.toml here or in a parent folder, or $BATON_WORKSPACE)")
    return p


def _add_run_commands(sub) -> None:
    p = sub.add_parser("init", help="write a ready-to-edit workspace (baton.toml, common.md, example task, "
                                    "example Claude settings)")
    p.add_argument("dir", nargs="?", default="task-auto", help="folder to create (default: ./task-auto)")
    p.add_argument("--force", action="store_true",
                   help="write into a folder that is not empty, overwriting the template files in it")
    p.set_defaults(func=cmd_init)
    p = _with_config(sub.add_parser("run", help="run every unfinished task"))
    p.add_argument("--only", metavar="ID", help="run just this task, even if it finished before")
    p.add_argument("--force", action="store_true",
                   help="with --only: run the task even when a verifier it depends on is not done")
    p.add_argument("--provider", metavar="NAME",
                   help="begin from this provider instead of start_provider (also for tasks already mid-attempt)")
    p.add_argument("--follow", action="store_true",
                   help="keep running when the queue is empty: pick up new task files and approvals "
                        "until `baton stop` or Ctrl+C")
    p.set_defaults(func=cmd_run)
    p = _with_config(sub.add_parser("plan", help="check everything and show the plan; run nothing"))
    p.add_argument("--provider", metavar="NAME", help="show the plan as `run --provider` would run it")
    p.set_defaults(func=cmd_plan)
    p = _with_config(sub.add_parser("stop", help="finish the current task, then end the running baton cleanly"))
    p.set_defaults(func=cmd_stop)


def _add_view_commands(sub) -> None:
    p = _with_config(sub.add_parser("watch", help="live view of the current attempt's agent output"))
    p.add_argument("-n", "--lines", type=int, default=30, help="lines of history to show first (default 30)")
    p.add_argument("--raw", action="store_true", help="show the raw event lines instead of a readable summary")
    p.add_argument("--no-follow", action="store_true", help="print the tail and exit")
    p.add_argument("--lane", metavar="NAME",
                   help="with [run] lanes > 1: follow this lane (default: the first busy one)")
    p.set_defaults(func=cmd_watch)
    p = _with_config(sub.add_parser("status", help="show the summary and the last log lines"))
    p.add_argument("-n", "--lines", type=int, default=10)
    p.set_defaults(func=cmd_status)


def _add_decision_commands(sub) -> None:
    p = _with_config(sub.add_parser("approve", help="release the tasks held by an approval checkpoint"))
    p.add_argument("id")
    p.add_argument("--note", help="optional comment kept in the run state")
    p.set_defaults(func=cmd_approve)
    p = _with_config(sub.add_parser("reject", help="block the tasks held by an approval checkpoint"))
    p.add_argument("id")
    p.add_argument("--note", help="what is wrong; shown on every blocked task")
    p.set_defaults(func=cmd_reject)


def _add_operator_commands(sub) -> None:
    p = _with_config(sub.add_parser("mark", help="set a task's status by hand (a verifier that was right, work "
                                                "done outside the run)"))
    p.add_argument("id")
    p.add_argument("status", choices=["done", "partial", "failed", "blocked"])
    p.add_argument("--note", help="why; kept in the state, the log and the SUMMARY")
    p.add_argument("--result", metavar="FILE", help="use this file as the task's result file")
    p.set_defaults(func=cmd_mark)
    p = _with_config(sub.add_parser("retry", help="clear a task's state and run just that task again"))
    p.add_argument("id")
    p.add_argument("--force", action="store_true", help="run it even when a verifier or checkpoint before it is open")
    p.add_argument("--provider", metavar="NAME", help="begin from this provider")
    p.set_defaults(func=cmd_retry)
    p = _with_config(sub.add_parser("resume", help="interrupt one unfinished attempt and continue preserved work"))
    p.add_argument("id")
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser("providers", help="provider state: `baton providers reset NAME` clears a stale limit")
    psub = p.add_subparsers(dest="providers_cmd", required=True)
    pr = _with_config(psub.add_parser("reset", help="forget a provider's limit and disabled flag"))
    pr.add_argument("name")
    pr.set_defaults(func=cmd_providers)
    p = sub.add_parser("classify", help="classify a saved CLI output (limit / transient / auth / ...)")
    p.add_argument("provider", choices=["claude", "codex"])
    p.add_argument("stdout")
    p.add_argument("--stderr")
    p.add_argument("--exit-code", type=int, default=1)
    p.add_argument("--now", help="ISO time to resolve relative reset times against")
    p.add_argument("--timezone", help="time zone for reset times without one")
    p.set_defaults(func=cmd_classify)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="baton", description=__doc__)
    ap.add_argument("--version", action="version", version=f"baton {__version__}")
    sub = add_review_commands(ap.add_subparsers(dest="cmd", required=True))
    _add_run_commands(sub)
    _add_view_commands(sub)
    _add_decision_commands(sub)
    _add_operator_commands(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .state import DiskFull, StateError
    try:
        return args.func(args)
    except StateError as e:             # a damaged run.json with no backup: say what to do, no traceback
        print(f"baton: {e}", file=sys.stderr)
        return 2
    except DiskFull as e:
        print(f"baton: {e}; free some space and run baton again", file=sys.stderr)
        return 6
    except BrokenPipeError:         # `baton status | head`: the reader left; not an error
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
