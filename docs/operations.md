# Operating a night

## Commands

```bash
baton init ~/work/task-auto   # scaffold a workspace (see the quick start)
baton plan      # preflight + the order and models it would use; runs nothing
baton run       # run every unfinished task
baton run --follow            # keep running when the queue is empty: pick up new task files
                              # and approvals until `baton stop` or Ctrl+C
baton run --provider codex    # begin from codex, whatever the saved state says
baton run --only 10-example   # one task, even if it finished before
                              # (refused while a verifier it depends on is not done; --force overrides)
baton status    # the summary (rebuilt from the state), what to do next, and the last log lines
baton watch     # live, readable view of the current attempt's agent output (--raw for the events)
baton stop      # from another terminal: finish the current task, then end cleanly
baton approve ID               # release the tasks held by an approval checkpoint
baton reject ID --note "..."   # block them, with the note on every blocked task
baton mark ID done --note "why"          # set a task's status by hand (also failed, blocked); --result FILE
baton retry ID                           # clear one task's state and run just that task again
baton providers reset claude             # forget a stale limit or disabled flag
baton review --only ID | --pending       # read-only review by the OTHER provider (--allow-same: see below)
baton review-fix --only ID               # one round of fixes for the review's [blocker] findings
```

`baton` looks for `baton.toml` in the current folder and then in the folders above it, so
`baton status` works from a subfolder; `BATON_WORKSPACE=/path/to/task-auto` (a folder or the
file) names it from anywhere, and `-c PATH` always wins.

### When a run is stuck on something that is not the code

A verifier that was right to complain about a broken environment, a test that fails for a reason
no task owns, work you finished by hand: the run gates or skips what depends on it, and nothing
should make you edit `state/run.json`. `baton status` ends with "what to do next" and names the
command for each stuck task:

- `baton retry ID` clears the task (status, notes, counters; the old `attempt-NN` files move to
  `results/<id>/earlier-N/`) and runs only it. It refuses a task behind an open verifier or
  checkpoint unless you add `--force`, like `run --only`.
- `baton mark ID done|failed|blocked --note "why"` records your decision: `overridden_by =
  operator` and the note in the state, the log and the SUMMARY (the row says `[set by operator]`),
  the first line of `results/<id>.md` rewritten (the agent's own file is kept in
  `results/<id>/result-before-mark-N.md`; `--result FILE` replaces it with yours). `mark ... done`
  also sets the tasks it had gated or skipped back to pending; `baton run` then continues.
- `baton providers reset NAME` clears a provider's limit and disabled flag, for a limit baton
  could not read the end of.

While a run is active, `mark` and `providers reset` are queued in `state/ops/` and the run applies
them between tasks (never to a task whose attempt is running); `retry` needs the run stopped,
because it starts a run of its own. Only the run writes `run.json` while it is going.

Run it inside tmux (`tmux new -s baton`, then `baton run`, detach with
Ctrl+b d) and keep the laptop on power. Sleep/suspend stops the run in the middle of an attempt, so
turn it off for the night: `systemd-inhibit --what=sleep:idle baton run` on Linux, `caffeinate -i baton run`
on macOS (or the machine's power settings). A second `baton run` in the same
folder refuses to start. Ctrl+C stops cleanly: the agent is stopped, its
work committed as WIP, the state saved; the next `baton run` continues.
`baton stop` is the gentler way: the attempt in progress is left alone, the run
ends after the task it is on (exit code 0), and a wait for a usage-limit reset is
cut short with the task left resumable.

### Before you launch

Mistakes made at launch cost whole nights in real runs. A checklist:

- **Start it from your own terminal, in a fresh tmux session** (`tmux new -s baton`; check with
  `tmux ls` that the name is not taken). Never launch it from inside another agent's sandbox: Codex's
  own sandbox (`bwrap`) cannot start there, and every Codex task fails.
- **Stop the machine from sleeping** (`systemd-inhibit --what=sleep:idle baton run`, or `caffeinate -i`),
  and keep it on power. A closed terminal (SIGHUP) stops the run cleanly, but a suspended laptop
  stops it in the middle of an attempt.
- **Update the base branch first.** baton never fetches or pulls, so a task starts from the *local*
  `default_base`. Pull it yourself, and update baton itself only between runs, never during one.
- **Read `baton plan`** to the end: warnings (clock zone, heavy models, lanes that cannot overlap,
  Codex sandbox, git identity) are cheaper to fix now than at 3 a.m.
- **Do not edit `baton.toml` or task files mid-run** except new task files with `--follow`: baton reads
  the config when it starts. Change a setting by `baton stop`, edit, `baton run`.
- **Keep your own quota in mind.** The night can use most of your weekly Claude allowance, and when
  your own quota runs out nothing can be started or fixed. Queue the next tasks before it gets low, or
  use `baton run --follow` so new task files are picked up while you sleep.

### How do I know the night went wrong?

- **Exit code** (table below). Wrap it if you want a signal: `baton run; echo "baton exit $?" | mail -s baton you@example.com`.
- **`baton status`** prints `results/SUMMARY.md` and the last log lines: which tasks are `done`, `partial`,
  `failed`, `blocked`, `gated`, or waiting for an approval, and the one-line reason.
- **`logs/run.log`** says what happened and why (`switch`, `sleeping until`, `emergency brake`, `ERROR ...`).
  A line containing `ERROR` or `no limit pattern matched` means read the attempt's `.out` and `.err`.
- **`results/SUMMARY.md` first lines** show when it was last written; if that time is hours old while the
  night should be running, the process is dead or asleep (`baton status`, `ps`, `tmux attach`).
- **A hook.** Set `[run] on_event` to a command (ntfy, a webhook, Telegram) and baton runs it when a
  task ends, the run sleeps for long, ends, is stopped or dies (see [environment.md](environment.md)).
  Watch for the *absence* of `run_end` too: a baton killed with SIGKILL or a power cut cannot report itself,
  so the next run sends `run_died` for the work it finds left mid-way.

### What a night costs

Most of the tokens are *context read again at every step* (cache reads are around 98 % of the total in
measured runs), so cost is roughly context size times the number of steps, not the size of the change.

- Small, specific tasks on a middle model (Sonnet or `gpt-6.1-sol` at medium/high effort) cost a fraction
  of the heavy ones, and the heavy ones rarely do better on routine work. Keep Opus and `xhigh` for
  verifiers and the hardest task; `baton plan` warns about `xhigh` and the heaviest Codex models.
- Keep prompts and the files they point to small. A large discovery file that every task reads is paid
  for in every step of every task; split it per task group.
- Tell the agent to read each file once and only the part it needs, to pipe test, lint and build
  output through `tail -n 40`, to run the changed tests during the task and the full suite once at
  the end. The example `common.md` already says so; keep it in yours.

### Cross-provider review

Optional, and only when you ask: `baton run` never starts a review or a fix, and a review gates nothing
(a task that depends on a reviewed task starts whatever the verdict).

```bash
baton review --only 10-api      # any done task, with or without `review = true` in its file
baton review --pending          # every task with `review = true` that is done and has no review yet
baton review --only 10-api --allow-same
baton review-fix --only 10-api  # one round on the [blocker] findings; --force allows another
```

- **Who reviews.** The provider that did *not* implement the task (baton records the implementer when a task
  finishes). If that provider is limited, disabled, missing or has no model for the task's rung, the command
  fails and says why and until when; it does not fall back to the implementer unless you pass
  `--allow-same`. A task whose implementer baton does not know (marked done by hand) needs `--allow-same`.
- **What the reviewer reads.** The task's `## Goal` and `## Acceptance` (the whole text when it has neither),
  `results/<id>.md`, and the diff of the task branch against its base (cut at 150,000 characters; the file
  list is always complete). It never sees `verifiers/` or their results. The checkout may be on another
  branch: the reviewer reads files at the reviewed state with `git show <branch>:<path>`.
- **Read-only.** Claude runs with deny rules for editing the repository and everything in the baton folder
  except `reviews/`, and for the usual file- and repository-changing commands; Codex runs with `--sandbox
  read-only`, no network and no extra writable directory (a config whose `extra_args` change the sandbox is
  refused). Prefix rules can be bypassed with an interpreter, so baton also compares `HEAD`, the branch, the
  refs, the stash and the uncommitted changes before and after. If anything differs the review is thrown
  away, marked `blocked` (`reviews/<id>.blocked.md` says what changed) and nothing is reset or cleaned.
- **The review.** `reviews/<id>.md`; the first line is `VERDICT: approve|changes|reject`, then one finding
  per line tagged `[blocker]`, `[should]` or `[nit]` with `path:line` and the reason. Parsing is strict (case
  and spaces aside): a text that does not start with the verdict line is kept as written and marked
  `invalid`. An older review moves to `reviews/history/`. `--pending` skips a task whose last review is
  `invalid` or `blocked`; run it again with `--only`.
- **The fix.** `review-fix` needs a valid review with at least one `[blocker]`. The provider that implemented
  the task (never the other one) fixes every blocker, may fix `[should]` findings, ignores `[nit]`, stays
  inside the task's Goal, may decline a finding with a reason, commits on the task branch and never pushes.
  A limit is waited out like for a normal task (up to `[run] max_sleep_hours`, else the command exits
  blocked and the round is not used up). Baton appends a `## Review fix` section to `results/<id>.md` with
  each finding as `fixed` or `declined: <reason>`. One round: `fix_round` is recorded before the agent
  starts and a second run is refused unless `--force`. A new `baton review` never reopens it, and nothing
  re-reviews automatically.
- **Seeing it.** `baton plan` and `baton status` list, for the tasks involved, `review none|pending|approve|
  changes|reject` (also `invalid`, `blocked`) and `fix not run|done|blocked`.
- **Exit codes** of both commands: 0 done (also "nothing to review"), 2 refused or bad usage, 3 blocked (the
  reviewer changed the repository, hit its limit or produced nothing; the fixer left a blocker unanswered),
  4 a baton run is active (both commands take the run lock).

### Exit codes

| code | meaning |
|---|---|
| 0 | the run finished its queue, or `baton stop` ended it cleanly (tasks may be left) |
| 1 | preflight failed (`baton plan` shows why) |
| 2 | bad usage, config or task file; or a damaged `run.json` with no backup |
| 3 | the run was stopped: Ctrl+C, SIGHUP, SIGTERM, the emergency brake, a service that stayed down, a missing required environment variable |
| 4 | another baton run is active in this folder |
| 5 | `--only`/`retry` refused because a verifier or checkpoint before the task is open (use `--force`) |
| 6 | the disk is full |

On a full disk baton stops at once, writes nothing more, and `state/run.json` stays as its last good save
(`run.json.bak` keeps the one before it and is used when `run.json` is found damaged); free space, then run
`baton run` again. Old `results/<id>/attempt-*.out` files and `logs/` are safe to delete.

## Reading the summary

`results/SUMMARY.md` (and `baton status`, which rebuilds it from `state/run.json` so a state changed by
`baton mark` shows at once) has, from the top:

- **Needs from you**: what the tasks' result files list under `## Needs from you` (every prompt asks
  for it): new permission keys, environment variables, migrations, manual steps. Absent when no task
  asked for anything.
- the task table. The `note` of a partial, blocked or failed task ends with the first line of its
  reason in the result file (the section called Issues, Blockers or Reason, else the first prose after
  STATUS). A task that was in the middle of an attempt when the run ended is `interrupted` (the next
  run continues it); one whose status you set with `baton mark` says `[set by operator]`.
- **Branches**: each task's branch and commit count. A verifier's tests are on its own `verify/<id>`
  branch, not in the implementation branch. At the end of a run `results/BRANCHES.md` lists, per
  repository, the branches the run left, which pairs would conflict (computed in memory with
  `merge-tree`; nothing is merged or pushed) and a suggested order.
- **Providers**: a limit that has passed reads "available". Every time is ISO with the UTC offset and
  how far away it is: `2026-10-31 18:32 +03:30 (in 2h10m)`.

A second `baton run` in a folder with a run says who holds it: `already active (pid 4242, since
2026-11-01 02:10 +03:30)`. The lock itself is released by the system when that process dies, so a
crashed run never leaves a lock to clean up.

Before each new attempt the task's front matter (`providers`, `rung`/`work_type`, `timeout_min`,
`max_turns`, `priority`, the text) and these settings of `baton.toml` are read again, and what
changed is logged: the routing table, timeouts and retry limits, `verifier_test_paths`, and each
provider's `extra_args`, `env`, `settings`, `permission_mode`, `effort_flag`, `add_dirs`, `sandbox`,
`profile`. A file that no longer parses is ignored (the log says why, once). A provider's `bin` or
`enabled`, the folders and `lanes` need the run to be started again.

## What happens when an attempt ends

| outcome | baton does |
|---|---|
| result file written | finish the task |
| usage limit | mark the provider limited until the reset (+ margin), commit leftovers, continue on the other provider or sleep |
| 429 / overloaded | wait, retry on the same provider (a few times) |
| authentication error | disable that provider for this run |
| quick exit, unknown reason | wait and retry; after `max_quick_unknown` (3) such exits of **the same task** that task is `failed` and the other tasks go on. The run-wide emergency brake is only for a shared cause: the program cannot be started (exit 126/127) or that many **different** tasks in a row. The raw output is kept in `state/fixtures/` |
| long run, no result (timeout, crash) | commit leftovers, retry once in a fresh session |
| agent left the repo on another branch | stop the run |

## Limit detection

The part that broke the hand-written scripts this grew out of. Evidence is
read most-structured first:

- **Claude** (`claude -p --output-format stream-json`): `rate_limit_event`
  with `resetsAt`; then the error text (`You've hit your session limit ·
  resets 10:10pm (Europe/Berlin)`, weekly and per-model variants); then, because
  a weekly limit can exit with nothing on stdout, the session transcript under
  `~/.claude/projects/`. An "approaching your limit … wrap up" ending also
  counts.
- **Codex** (`codex exec --json`): `error` / `turn.failed` events (`try again
  at 1:52 PM.`, `at Oct 9th, 2026 11:19 PM`, `in 2 hours 5 minutes`, curly
  apostrophes); then `usage_limit_exceeded` with the full window's
  `resets_at` from the rollout under `~/.codex/sessions/`.
- Only error text is scanned, never the agent's own messages — an agent
  writing rate-limit code is not mistaken for a limit.
- A reset time without a zone is read in `run.timezone`, which defaults to this machine's
  zone; set it only if the CLI prints another zone (`baton plan` warns when it differs from
  the machine's). An unreadable one gets a cooldown (`unknown_limit_cooldown_min`).

Check a captured output against the parser:

```bash
baton classify claude results/10-example/attempt-03.claude.out --exit-code 1
baton classify codex  state/fixtures/<file>.out --timezone Europe/Berlin
```

When baton meets a message it does not recognise, it waits a cooldown, logs `no limit pattern matched`,
and keeps the raw output in `state/fixtures/`. Send that file (or add it to `tests/fixtures/limits/` with a
row in `tests/test_limits.py`) so the next version reads it.

## Housekeeping

baton never deletes or rotates its own files. `results/<id>/attempt-NN.*` (prompt, `.out`, `.err`),
`logs/run.log` and `results/metrics.jsonl` grow with every attempt; a long run with big agent output
can reach hundreds of megabytes. Prune them between runs, never during one:

```bash
find results -name 'attempt-*.out' -mtime +14 -delete     # old raw agent output
: > logs/run.log                                          # or keep a copy first: cp logs/run.log logs/run.$(date +%F).log
```

Keep `results/<id>.md`, `results/SUMMARY.md` and `state/` (the next run reads them).

## Metrics

`results/metrics.jsonl` has one line per attempt: task, work type, rung,
provider, model, effort, seconds, outcome, limit window and reset, session id,
`switched_from` (a switch is **not** an escalation), and token usage
(including cache reads). Session ids match ccusage's session view.
