# Parallel lanes (experimental)

By default baton runs one task at a time. With `lanes > 1` it runs several at once,
so one provider's idle time is used while the other works, and repositories do not
queue behind each other. Off by default; treat it as experimental until you have run it
a few nights.

```toml
[run]
lanes = 2                  # at most two tasks at once (default 1)

[providers.claude]
max_concurrent = 1         # attempts at once on this provider (default 1; see below)
[providers.codex]
max_concurrent = 1
```

```markdown
+++
repo = "../shop-api"
lane = "shared-db"         # optional: tasks with the same lane never overlap, even in other repositories
+++
```

- **A lane is a repository.** Its key is the repository's git top level, so `../api` and
  `/abs/api/src` are one lane. A work tree has one checkout, so two tasks in the same
  repository never run together; neither do two tasks with the same `lane` value (use it for
  repositories that share a test database, a port or a build cache).
- **One scheduler.** A limit seen in one lane stops *new* attempts on that provider in every
  lane. Attempts already running on it are left to end on their own. A task whose provider
  was limited goes back to the queue and continues on another provider, in queue order.
- **Slots.** `max_concurrent` caps attempts per provider. Two Claude sessions on one
  subscription burn the same five-hour window twice as fast, so the default is 1, and the most
  useful setting is `lanes = 2`: one task on Claude, one on Codex. A busy slot never sends a task
  to the other provider (an Opus-rung task is not moved to Codex because Claude is busy): it
  waits for the slot. Only a limit moves it. Use `providers = [...]` on tasks to spread them.
  With the defaults (`max_concurrent = 1`) tasks that can only use one provider (`providers = ["claude"]`,
  or every task if Codex is not enabled) never overlap, however high `lanes` is: they queue for
  that provider's single slot. To overlap them, give some tasks `providers = ["codex"]`, or raise
  `[providers.claude] max_concurrent` if your plan's limits allow it. `baton plan` warns when `lanes > 1`
  but nothing can overlap.
- **Order.** Ready tasks are still scanned in the same order (priority, then file name); the first
  one whose lane, repository and provider are free starts. Completion order depends on how long
  agents take, so `run.log` is interleaved; each line carries a `[lane]` prefix and
  `metrics.jsonl` rows have a `lane` field.
- **Emergency brake.** Same rule as in serial mode: `max_quick_unknown` quick unexplained exits of one
  task fail that task and the other lanes go on. A program that cannot be started (exit 126/127) or
  `max_quick_unknown` different tasks in a row stop every lane.
- **Stop.** Ctrl+C stops every agent, each lane commits its work as WIP, and the next run continues.
  `baton stop` starts nothing new and lets every busy lane finish its task.
- **See it.** `baton status` lists the running lanes, `baton watch --lane NAME` follows one of
  them (default: the first busy lane), and `baton plan` shows each task's lane.
- **`baton run --only ID`** runs one task and ignores `lanes`.

Security: a verifier's tests are uncommitted files in its repository while it runs. Codex has no read
restriction, so a Codex task running in *another* lane could read them if it is pointed at that
repository; keep `verifier_providers = ["claude"]` (the default) and give tasks you do not want near a
verifier `providers = ["claude"]`, or keep verifiers out of parallel runs (`lanes = 1`). `baton plan`
warns about this when `lanes > 1`, Codex and verifiers are all present.

Not done yet: two tasks in the same repository (that needs `git worktree` per lane),
a `prefer_free_provider` switch, and per-lane resource hints.
