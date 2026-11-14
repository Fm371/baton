# Design: parallel lanes

Status: implemented (experimental, `lanes = 1` by default), after 0.3.0. Written against 0.2.0 (572b44d).
Decisions taken for the open questions: `lanes` defaults to 1 (opt-in); on a limit, lanes already running on that
provider are left to end on their own; a busy slot never pushes a task to the other provider; the front-matter key
is `lane`. Where the code differs from the text below: the split is `Runner` plus `baton/lanes.py` (`LanesMixin`:
dispatcher and workers) instead of separate `Dispatcher`/`TaskRun` classes, `lanes = 1` keeps the old serial loop
(`Runner.serial_loop`) rather than going through the dispatcher, a worker's `run_task(task, provider)` keeps the
provider the dispatcher assigned and hands the task back on a limit/auth/sandbox failure, `TaskState.lane` and
`last_provider` were not added (the lane lives in `RunState.running`, the last provider is `TaskState.providers[-1]`),
and a task's repository is a second exclusion next to its lane so an explicit `lane` can never put two tasks in
one work tree. Not done: `git worktree` lanes, `prefer_free_provider`.

## Problem

baton runs one task at a time. `Runner.run()` takes an exclusive `flock` on
`state/lock`, walks `order_tasks()` in a single loop, and `run_task()` holds
the loop until that task ends. A second `baton run` in the same workspace is
refused ("another baton run is already active in this folder").

That leaves throughput on the table in two ways:

1. **Idle providers.** Claude and Codex have separate usage limits. While a
   20-minute Opus task runs on Claude, Codex sits idle even when a ready
   Codex-capable task exists in another repo.
2. **Idle repos.** A workspace often spans several repos (an API, an admin
   panel, a core library). Tasks in different repos rarely touch the same
   files, but they still queue behind each other.

Skip-ahead (work list item 6) fixes a narrower case: when the next task's only
allowed provider is limited, run another ready task instead of sleeping. It is
still one task at a time. This design is the next step: several tasks at once,
one per lane, under one scheduler.

## Goals

- Run tasks in different repos at the same time, up to a configured number.
- One scheduler and one copy of provider-limit state per workspace, so a limit
  seen by one lane stops every lane from starting new work on that provider.
- `lanes = 1` (the default) behaves exactly like baton with skip-ahead: same
  order, same logs apart from a lane prefix, same state file.
- Keep every existing guarantee: deterministic order, dependencies and verifier
  gates, never push, commit leftovers as `chore(...): wip, ...`, crash recovery from
  `state/run.json`, the emergency brake.
- No new dependencies.

## Non-goals

- Two tasks in the **same** repo at once. A git work tree has one checkout;
  `gitutil.checkout`, `commit_leftovers` and the "left on another branch" check
  all assume the repo is ours alone. Git worktrees could lift this later (see
  "Later").
- Several `baton run` processes cooperating through the state file. The flock
  stays: one run per workspace, with the parallelism inside it.
- Isolating shared resources outside git (ports, a local database, a shared
  `.venv`, build caches). Users can serialize such tasks with an explicit
  `lane` key (below).
- Distributing work across machines.

## Concepts

**Lane.** A serial executor bound to one key. A lane runs at most one task
at a time. By default the key is the task's repo, resolved to its git top
level (`git rev-parse --show-toplevel`), so `../api` and `/abs/path/api` land in
the same lane. A task may set `lane = "db"` in its front matter to put tasks
from different repos into one lane on purpose, for example two repos that
share a test database.

**Slot.** A provider's capacity for concurrent attempts,
`[providers.<name>] max_concurrent` (default 1). Running two Claude sessions
on one subscription burns the same five-hour window twice as fast, so the
default is one. With the defaults the most useful number of lanes is two:
one on Claude, one on Codex.

**Dispatcher.** The one scheduler. It owns `RunState`, the provider status
(`Scheduler`), the log, SUMMARY.md and the lane table. It is the only writer
of `state/run.json`.

**Worker.** A thread that runs one task's attempt loop (today's `run_task`
body) for one lane, and reports events back to the dispatcher. Workers never
write shared state directly.

## Process model

One process, the dispatcher on the main thread, one worker thread per busy
lane.

Why threads:

- A worker spends almost all its time in `Popen.wait()`, which releases the
  GIL. The Python side is light: reading result files, classifying output.
- Signal handlers must run on the main thread. The dispatcher stays there and
  forwards a stop to every live child process group.
- Shared state stays in memory behind one lock, with a single writer.

Rejected alternatives:

- *Several processes coordinating through `run.json`.* Every write becomes a
  read-merge-write under a file lock, limit state can go stale between
  processes, and crash recovery has to work out which process owned which
  task. All of that is avoided by having one owner.
- *asyncio.* It would mean rewriting `execute()`, `Clock.sleep_until` and the
  tests around an event loop for no gain over threads blocked in `wait()`.

## Dispatch loop

```
loop:
    handle finished/limited/stopped events from workers   (queue.get with timeout)
    if stop requested and no lane busy: break
    if stop requested: continue                            (graceful: wait for lanes)
    for task in ready_tasks():                             (skip-ahead's ordering)
        if lane_of(task) is busy: continue
        prov = pick_provider(task, now)                    (allowed, available, free slot)
        if prov: start worker(task, prov); take slot; mark lane busy
        if busy lanes == cfg.lanes: break
    if no lane busy:
        if no ready task: break                            (all done, blocked or skipped)
        wake = earliest reset among providers that some ready task may use
        sleep until wake (or stop), then continue
    else:
        wait for next worker event, or the earliest reset, whichever comes first
```

`ready_tasks()` is the same function skip-ahead introduces: tasks whose
dependencies are finished (`done` or `partial`), whose verifier gates are
`done` (unless `--force`), that are not finished and not running, sorted by
priority then file name. With `lanes = 1` the loop above reduces to
skip-ahead: pick the first ready task that has a free provider, otherwise
sleep until the earliest reset that would unblock a ready task.

Ordering stays deterministic for a given sequence of events: the dispatcher
always scans ready tasks in the same order and assigns the first free lane and
provider. What changes is that the order of *completion* depends on how long
agents take, so the log is interleaved. Each line gets a lane prefix:

```
[2026-10-26 14:02:11] [api] task T03: attempt 1 on claude (sonnet medium)
[2026-10-26 14:02:11] [admin] task T07: attempt 1 on codex (gpt-... medium)
```

### Picking a provider

Today `Scheduler.pick()` keeps one global `current` provider and stays on it
until it is limited ("sticky"). Stickiness exists so a task is not bounced
between tools and so prompt caches stay warm. With lanes there is no single
current provider, so stickiness moves to the task:

1. A task that already ran (resume, retry, after a limit) keeps its last
   provider while that provider is available and has a free slot.
2. Otherwise, the first provider in `start_provider` order that the task
   allows (per-task `providers`, `verifier_providers` for verifiers), is
   available, and has a free slot.
3. If an allowed provider is available but all its slots are taken, the task
   waits for a slot. It does not fall back to another provider just because of
   a busy slot, only because of a limit. This keeps model routing predictable:
   a busy Claude slot is not a reason to send an Opus-rung task to Codex.

   Open question: some users will want "use whichever is free". That can be a
   later `prefer_free_provider = true` switch.

`Scheduler` keeps `mark_limited`, `disable`, `available` and the reset
logic. `pick(now, allowed)` (0.2.0 already narrows by the task's
`providers`) becomes `pick_for(allowed, last, now, busy)`, and `current` is
kept in the state file only for backward compatibility.

The 0.2.0 "no fallback outside the list" rules move from `run_task` to the
dispatcher unchanged in meaning: a task whose allowed providers are all
disabled ends `blocked`; a task whose allowed providers are all limited past
`max_sleep_hours` ends `blocked`; otherwise it is simply not dispatchable
until the earliest reset among its allowed providers, and the dispatcher's
sleep target takes that into account. The log uses the item 5 wording
("allowed providers (claude) limited; codex not allowed for this task").

### Shared limit state

When a worker's attempt ends with `Outcome.kind == "limit"`, it sends a
`limited(provider, until, window)` event. The dispatcher marks the provider
limited for every lane. Other lanes already running on that provider are
**not** interrupted: they will hit the same limit and end on their own, and
killing them would throw away work for no gain. They are just not given a new
attempt on that provider. Same for `auth`: the provider is disabled for the
run and no new attempt starts on it.

A worker whose task hit a limit releases its lane and slot and returns the
task to the ready set (status stays `running`, attempts and `last_end` are
kept). The next dispatch pass decides where it continues, exactly like the
switch inside `run_task` today, but now another lane's task may get the free
provider first if it is ahead in order.

Transient errors (`overloaded`, 429 without a limit) stay local to the
worker: it pauses and retries the same provider, holding its lane and slot.

### Emergency brake

`quick_fails` stays a single counter for the run, because the failure it
guards against (a CLI that exits immediately for an unknown reason, burning
attempts) is usually global: a broken install, an expired login, a full disk.
Any lane's quick unknown exit increments it, any lane's real result resets it,
and reaching `max_quick_unknown` stops dispatching and stops all lanes.

## Dependencies, branches and verifiers

- **Dependencies across repos.** A task in `admin` that depends on one in
  `api` waits until that task is finished, as today. It is simply not ready,
  so its lane picks something else or idles.
- **Branch from a dependency's branch** (per-task `base`). The dependency and
  the dependent are in the same repo, so they share a lane and run in order.
  Nothing changes.
- **Hidden verifiers.** A verifier is an ordinary task for scheduling: it is
  in the lane of its repo and is ready once its dependencies are finished
  (`Runner.satisfied()`). Tasks behind it stay `gated` until it is `done`. A
  verifier depends on the task it checks, so the two never overlap, and
  `resolve_verifier_bases()` puts it on `verify/<id>` branched from that
  task's branch in the same repo, so it is in the same lane anyway.
- **Verifier test-path check.** `finish()` blocks a verifier whose diff
  against its `start_head` touches non-test files. That diff is only
  meaningful if nobody else commits to the repo while the verifier runs. Lane
  serialization guarantees it; this is one more reason the lane key must be
  the repo's top level and never finer.
- **Hiding with lanes.** The hiding rules are per session and do not depend on
  timing: each attempt gets its own `attempt-NN.claude.settings.json` from
  `hidden_paths()`, implementers are denied `verifiers/` and the verifiers'
  results, verifiers are denied `results/` and `tasks/`. Lanes add one new
  exposure: while a verifier runs in one lane, a Codex implementer in another
  lane could read the verifier's uncommitted test files in that repo's work
  tree, since Codex has no read restriction. It is the same documented gap as
  today (Codex can already read `verifiers/`), but the README's security model
  section should mention it. A cautious user can keep verifiers out of
  parallel runs with `verifier_providers = ["claude"]` (the default) and
  `providers = ["claude"]` on implementers in other repos.
- **`--only ID`.** Runs one task, so it never needs more than one lane. Gates
  are respected as today unless `--force`.

## Changes per module

| Module | Change |
|---|---|
| `config.py` | `[run] lanes` (int, default 1). `[providers.*] max_concurrent` (int, default 1). Validation: both ≥ 1. |
| `tasks.py` | Optional `lane` front-matter key (string, same charset as ids). `Task.lane_key` resolved at load: the `lane` value or the repo's git top level. |
| `scheduler.py` | Slot accounting (`busy[provider]`), `pick_for(...)`, and a pure `select(ready, status, busy_lanes, now)` used by skip-ahead and lanes alike. |
| `runner.py` | Split into `Dispatcher` (today's `run`, `_run`, `recover`, `save`, `write_summary`, signal handling) and `TaskRun` (today's `run_task`, `execute`, `commit_leftovers`, `finish` minus the shared writes). `self._child` becomes one child per worker. |
| `state.py` | `TaskState.lane` and `TaskState.last_provider`. `RunState.running: {task_id: {lane, provider, attempt, out}}` so `status` and `watch` can show live work. A `version: 2` field; version 1 files load with `running` empty. |
| `cli.py` | `baton status` lists running tasks per lane. `baton plan` shows the lane of each task and the configured lanes and slots. |

### Shared files

- `state/run.json`, `results/SUMMARY.md`: dispatcher only, after every event.
  This also covers work list item 4 (SUMMARY freshness), which should land
  first and be reused here.
- `logs/run.log`, `results/metrics.jsonl`: appended by workers through a
  dispatcher-owned lock, one whole line per write. `metrics.jsonl` rows get a
  `lane` field.
- `results/<id>.md`, `results/<id>/attempt-NN.*`: per task, no sharing.
- `state/fixtures/`: file names already include the task id.

### Things that break silently if not handled

- **`watch_repos` dirty warning in `finish()`.** It warns about uncommitted
  changes in every other watched repo. With lanes, a repo another lane is
  working in is dirty by design. Exclude repos whose lane is busy.
- **`recover()`.** After a crash, several tasks may be `running`. It already
  loops over all tasks and commits leftovers per repo, so it works, but it
  must run before any lane starts.
- **Signals.** On Ctrl+C the dispatcher sends SIGTERM to every live child
  process group, then waits for workers to finish their cleanup
  (`commit_leftovers`, `last_end`), then saves state once.
- **Graceful stop** (work list item 7). With lanes it means "start nothing
  new, let every busy lane finish its current task". The signal file or
  `baton stop` sets the same flag the dispatcher checks.
- **`baton watch`** (item 8). It reads `RunState.running` and follows one
  lane (`--lane NAME`), or the first busy lane by default.

## Configuration example

```toml
[run]
lanes = 2                 # at most two tasks at once, in different repos

[providers.claude]
max_concurrent = 1        # default; raise only if your plan's limits allow it

[providers.codex]
max_concurrent = 1
```

```toml
+++
repo = "../shop-api"
lane = "shared-db"        # optional: serialize with tasks in other repos that use the same test DB
work_type = "crud"
+++
```

## Testing plan

All with the existing fake CLIs and fake clock, no real `claude` or `codex`.

1. **Pure selection.** `select()` table tests: ready order, busy lanes, busy
   slots, limited providers, per-task allowed providers, sticky last provider.
   No threads.
2. **`lanes = 1` equivalence.** The whole existing runner suite runs with
   `lanes = 1` unchanged and must pass as is.
3. **Overlap.** Two repos, two tasks, fake agents that write start and end
   timestamps and wait on a barrier file. Assert both started before either
   ended, with `lanes = 2`, one on each provider.
4. **Same repo is serial.** Two tasks in one repo with `lanes = 2`: never
   overlap.
5. **Explicit lane key.** Two repos with the same `lane`: never overlap.
6. **Shared limit.** Lane A's agent reports a Claude limit; lane B's next task
   (Claude-capable) must not start on Claude and goes to Codex or waits.
7. **Dependencies across lanes.** B depends on A in another repo: B starts
   only after A is `done`; with A `blocked`, B is skipped.
8. **Verifier gates** with lanes: gated tasks wait, `--force` overrides.
9. **Crash recovery** with two `running` tasks in `run.json`.
10. **Stop.** SIGTERM with two busy lanes: both children get SIGTERM, both
    tasks get leftovers committed and `last_end` set, exit code 3.

To keep the threaded tests deterministic, the worker's `execute` is
injectable, and the dispatcher's wait uses a `queue.Queue` with the fake
clock's timeout instead of real sleeps.

## Rollout

1. **Refactor, no behavior change.** Split `Runner` into `Dispatcher` and
   `TaskRun` on top of skip-ahead. Run everything inline (no threads). The
   full suite passes unchanged.
2. **Lanes behind config.** Add workers, slots, `lanes` and `max_concurrent`.
   Default `lanes = 1`. Mark `lanes > 1` experimental in the README and
   CHANGELOG.
3. **Live view.** `RunState.running`, lane-aware `status`, `watch --lane`.
4. **Later.** Same-repo parallelism with `git worktree add` per lane (each
   lane gets its own work tree on its own branch; baton still never pushes),
   `prefer_free_provider`, and per-lane resource hints.

## Open questions for the owner

1. Default for `lanes`: keep 1 (opt-in), or default to the total number of
   provider slots (2 with both providers enabled)?
2. On a limit in one lane, should other lanes on the same provider be stopped
   immediately (saves a few wasted turns) or left to end on their own
   (proposed)?
3. Should a busy slot ever push a task to the other provider
   (`prefer_free_provider`), or only a limit (proposed)?
4. Name of the front-matter key: `lane` (proposed) or `group`?
