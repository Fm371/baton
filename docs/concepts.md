# Concepts

How baton thinks: the workspace, the switching rule, rungs, and what it never does.

## The workspace

```
task-auto/
├── baton.toml        config: providers, routing table, limits
├── common.md         rules prepended to every task prompt
├── tasks/            one Markdown file per task
├── verifiers/        optional: hidden verification tasks and built-in checks
│                     (see verifiers.md)
├── results/          <task>.md written by the agent, SUMMARY.md, metrics.jsonl,
│                     <task>/attempt-NN.<provider>.{prompt.md,out,err}
├── state/run.json    where the run is; survives crashes and reboots
└── logs/run.log
```

## The switching rule

1. Start on `start_provider` (Codex by default) and **stay on it until it is
   limited**. A provider coming back is not a reason to switch a task that is
   already running; every switch is a cold start. The saved `current` provider
   only matters while a task is mid-attempt: a new run with none begins from
   `start_provider` (`baton run --provider codex` overrides it, mid-attempt tasks
   included).
2. When the current provider is limited, continue on the other one. The next
   task that has not started yet goes back to `start_provider` as soon as it
   is available again (`return_to_start_provider = false` keeps the old
   behavior of staying until the other one is limited too).
3. When **both** are limited, sleep until the **earliest** reset and continue
   on that provider. The two subscriptions' tokens aren't comparable; the
   clock is.
4. **Back mid-task** (`return_mid_task`, **experimental, off by default**; not with `lanes > 1`): while a
   task runs on the other provider and `start_provider` resets at a time read from its
   limit message, baton stops the attempt at the next check (`return_check_seconds`, 60),
   commits what was left as WIP and continues the same task on `start_provider` with the
   previous agent's notes. A cooldown guess (no readable reset time) never stops an attempt.
   An earlier version of this switch failed once in a real run, for a cause nobody found, and the
   version here is tested only with fake CLIs. Turn it on (`return_mid_task = true`) on a low-risk night first.
   A task can opt out (or in) on its own with `return_mid_task = false` in its front matter, for example a
   large migration that is costly to stop halfway; `baton plan` shows `return: off` for it.
   A limit that has already passed is dropped when the state is loaded, so neither `baton plan` nor
   `SUMMARY.md` shows `limited until <a time in the past>`.

Weekly limits need no special case: a reset days away simply loses to the
other provider's five-hour reset.

A task that moves to the other tool keeps its **rung** — the same row of the
routing table, never a lower one. The receiving agent is told why the task
moved and to read `git log` first, and receives the notes the previous agent kept in
`results/<id>.progress.md` (plan, done, next, open questions; every prompt asks for
them); whatever the previous agent left
uncommitted is committed as `chore(<task>): wip, … (unverified)` (never stashed).

## What a task is

A task is one Markdown file in `tasks/` with a short TOML header (which repository, what kind of work, what it
depends on) and a body that says what to build and how to know it is done. baton runs it in a fresh agent
session on its own branch and expects a result file back: `results/<id>.md`, whose first line is
`STATUS: done|partial|blocked`. baton decides the final status itself from that line and the commits the branch
got. Details: [tasks.md](tasks.md).

## Rungs and work types

A **rung** is one step of a ladder of models, with the equivalent on each provider (for example `r3` is
`sonnet medium` on Claude and `gpt-6.1-sol medium` on Codex). A **work type** (`crud`, `refactor`, `security`,
...) maps to a rung in `baton.toml`, so a task file says what the work is and the table decides the model.
When a task moves to the other provider it keeps its rung. See [configuration.md](configuration.md#routing).

## Checks you can put around a task

- **Approval checkpoints** (`kind = "approve"`): the run waits for a human decision before the tasks behind
  them start. [tasks.md](tasks.md#approval-checkpoints)
- **Verifiers**: an independent agent, or a built-in check, that looks at someone else's work without seeing
  how it was done, and gates what depends on it. [verifiers.md](verifiers.md)
- **Services and required environment**: a database that must be up, a variable that must be set, checked
  before an attempt is spent. [environment.md](environment.md)

## Parallel lanes

By default one task runs at a time. With `lanes > 1` tasks in different repositories run together, one lane per
repository. Experimental. [lanes.md](lanes.md)

## What baton never does

- It never pushes, resets, cleans, rebases or stashes (the one exception is the opt-in
  `existing_branch = "rebase"`, which rebases a task branch onto its dependency and aborts on a conflict). It
  never merges your work into your branches: you review the task branches and merge them yourself.
- It never commits on your current branch: a task works on its own branch (`baton/<id>` unless it names one),
  and every repository is put back where it was when the run ends.
- It never fetches or pulls. A task branches from the *local* base branch.
- It never rotates or deletes its own logs and attempt output ([operations.md](operations.md#housekeeping)).
- It does not sandbox the agent. The agent is limited only by the tool's own settings or sandbox
  ([safety.md](safety.md)).
- It does not read the agents' private data or call any usage endpoint: it only runs the official `claude`
  and `codex` binaries with your login, and reads their output.
