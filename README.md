# baton

Run a folder of coding-agent tasks overnight, unattended. When Claude Code hits its usage limit the work
continues on Codex, and the other way round. You write tasks as Markdown files; baton runs them one by one on
their own git branches, hands a half-done task to the other tool with notes, sleeps until a limit resets,
and leaves you a summary in the morning. It never pushes and never merges: you review the branches.

```
task-auto/                 your workspace (keep it outside the repositories it works on)
├── baton.toml             providers, routing table, limits
├── common.md              rules prepended to every task prompt
├── tasks/                 one Markdown file per task
└── results/  state/  logs/   what baton writes: SUMMARY.md, run.json, run.log
```

**Status:** version 0.4.0, pre-1.0, MIT licensed, used on a few real projects. It is tested against fake
`claude` and `codex` programs; how the real tools behave is verified only by those real runs (see
[Known limitations](#known-limitations)). Behaviour changes since 0.3.0 are listed in the
[CHANGELOG](CHANGELOG.md): unknown keys in `baton.toml` are now an error, a task without `branch` works on
`baton/<id>`, and `return_mid_task` is off by default.

## Why

A long agent task fails in boring ways: the usage limit hits at 2 a.m., a half-finished change is left
uncommitted, a green test suite proves the wrong feature, a verifier is told how the code was written. baton
exists to handle these without you: it reads the limit message, waits for the real reset time or moves the
task to the other tool (same model tier, with the previous agent's notes), commits whatever was left as a WIP
commit, gates tasks behind independent verifiers, and stops cleanly when you tell it to.

## Install

Python 3.11+, no runtime dependencies. The package is `baton-runner`; the command is `baton`.

```bash
git clone https://github.com/fm371/baton ~/tools/baton
pipx install ~/tools/baton          # or: pip install --user ~/tools/baton
baton --version
```

You also need `git` and the tools it drives, installed and logged in: `claude` (Claude Code) and, for the
switch, `codex`. With only one of them, set `enabled = false` on the other provider.

## Five-minute start

```bash
baton init ~/work/task-auto         # a workspace from the template
cd ~/work/task-auto
cp claude-settings.example.json claude-settings.json   # the rules that limit the Claude agent: read them
$EDITOR baton.toml                  # default_base = "main"; check the model names
$EDITOR tasks/10-example.md         # point repo = "..." at a clean git repository
baton plan                          # checks everything, shows the order and models, creates nothing
baton run                           # runs every unfinished task
baton status                        # the summary, and what to do next
```

The full walk-through, with a throwaway repository and a real task, is in
[docs/quickstart.md](docs/quickstart.md).

## The commands you use

| command | what it does |
|---|---|
| `baton plan` | preflight: checks the tools, repositories, branches, models, sandbox and config; prints the order and the model each task would use; runs nothing |
| `baton run` | runs every unfinished task (`--follow` keeps going and picks up new task files and approvals; `--only ID` runs one task) |
| `baton status` / `baton watch` | the summary table and what to do next / a live view of the current agent's output |
| `baton stop` | from another terminal: finish the current task, then end cleanly |
| `baton approve ID` / `baton reject ID` | decide a human checkpoint |
| `baton mark ID done` / `baton retry ID` | fix up a stuck task by hand instead of editing the state |
| `baton review --only ID` / `baton review-fix --only ID` | optional: a read-only review of a done task by the other provider, then one round of fixes for its blockers (only when you run them) |

Everything else (`providers reset`, `classify`, exit codes, the night checklist) is in
[docs/operations.md](docs/operations.md).

## A task

```markdown
+++
repo = "../my-repo"           # relative to baton.toml, or absolute
work_type = "crud"            # a row of the routing table: decides the model on each tool
depends_on = ["05-schema"]    # optional
+++
## Goal
Add the widgets list endpoint.

## Acceptance
- GET /widgets returns the user's widgets, newest first; tests cover empty and paged results.
```

The agent works on the branch `baton/<task id>`, writes `results/<id>.md` (first line
`STATUS: done|partial|blocked`), and baton decides the final status from that line and the commits.

## Optional: a second opinion on a finished task

Add `review = true` to a task header, then, after the run, ask the **other** provider for a read-only review
and, if it found blockers, for one round of fixes by the provider that wrote the code:

```bash
baton review --pending               # every done task with review = true that has no review yet
baton review --only 10-widgets       # any done task, even without review = true
baton review-fix --only 10-widgets   # one round; addresses the [blocker] findings only
```

The review is saved as `reviews/<id>.md` (first line `VERDICT: approve|changes|reject`, findings tagged
`[blocker]`, `[should]` or `[nit]`). Neither command ever runs from `baton run`, a review never gates other tasks,
and the reviewer cannot change your repository (baton checks the tree before and after and discards the review if
anything moved). Details: [docs/operations.md](docs/operations.md), [docs/safety.md](docs/safety.md).

## The switching rule, in four lines

1. Start on `start_provider` (Codex by default) and stay on it until it is limited.
2. When it is limited, continue on the other one; the next new task goes back to the first as soon as it is
   available again.
3. When both are limited, sleep until the earliest reset.
4. A task that changes tool keeps its model tier, gets the previous agent's notes, and whatever was left
   uncommitted is committed as a WIP commit (never stashed).

## What baton does not do

It does not push, merge, reset, clean or stash. It does not commit on your current branch, and it puts every
repository back where it was when the run ends. It never runs `git fetch`: update your base branch first.
**It does not sandbox the agent**: the agent is limited only by the allow/deny rules in your
`claude-settings.json` and by Codex's own sandbox, and runs unattended with your user's rights. Read
[docs/safety.md](docs/safety.md) before the first real night; run baton as a separate user or in a container.

## Documentation

| | |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | zero to a first successful run |
| [docs/concepts.md](docs/concepts.md) | the switching rule, rungs, what baton never does |
| [docs/tasks.md](docs/tasks.md) | task files, branches, approval checkpoints |
| [docs/verifiers.md](docs/verifiers.md) | independent checks and the built-in ones |
| [docs/configuration.md](docs/configuration.md) | every key of `baton.toml` and of a task header |
| [docs/operations.md](docs/operations.md) | commands, the night checklist, exit codes, limit detection, housekeeping |
| [docs/environment.md](docs/environment.md) | sandbox limits, services, notifications, required variables, paths |
| [docs/safety.md](docs/safety.md) | what is enforced and what is not |
| [docs/lanes.md](docs/lanes.md) | parallel tasks (experimental) |
| [docs/troubleshooting.md](docs/troubleshooting.md) | the messages you will meet and what to do |
| [docs/development.md](docs/development.md) | running the tests, how the fakes work |
| [docs/design/](docs/design/) | proposals not built yet |

## Known limitations

- **Real-tool behaviour is only partly verified.** The tests use fake CLIs. Real runs have shown the
  switching, WIP commits and handoff work; what only fake runs have exercised is `lanes > 1`,
  `return_mid_task`, the sandbox and network probes for Codex, and the newest limit-message parsing. Run a
  low-risk night before trusting them.
- **The reviewer's read-only guarantee is only checked with fakes.** `baton review` denies writes through
  Claude's settings and Codex's read-only sandbox, and compares the repository before and after; a command
  that an interpreter hides from the deny rules is caught only by that comparison, and a content change to an
  untracked file that keeps its size and time is not seen.
- **Agents are limited only by the tool's settings or sandbox** ([docs/safety.md](docs/safety.md)). `results/`
  is writable by every agent, so a task can in principle read or overwrite another task's result files.
- **A limit message baton cannot read** is handled with a cooldown and a `no limit pattern matched` warning,
  not a reset time. Send the saved output (`state/fixtures/`) so it can be added to `tests/fixtures/limits/`.
- **Pre-existing test failures are not tracked per task.** Only `repo-suite` has a baseline, so an unrelated
  failing test can make later tasks report `partial`. A design for a per-task baseline is in
  [docs/design/baseline-failures.md](docs/design/baseline-failures.md); it is not built.
- **Not built, design only:** resuming the same agent session after a reset
  ([docs/design/resume-same-session.md](docs/design/resume-same-session.md); every resume is a fresh session
  with the previous agent's notes), a per-run budget and fallback policy, protected paths per repository,
  and two tasks in the same repository at once. Proactive wrap-up before a limit and escalation to a higher
  rung do not exist either.
- **Logs and raw agent output are never rotated** ([docs/operations.md](docs/operations.md#housekeeping)).
  A very large `.out` file is read from its tail, but still sits on disk until you prune it.
- **One run per folder.** A second `baton run` in the same workspace refuses to start.
- **Linux is the tested platform.** macOS should work (baton uses POSIX calls only) but is untested;
  Windows is unsupported.

## Contributing

Tests are plain `unittest` with fake `claude` and `codex` programs; see
[docs/development.md](docs/development.md) and [CONTRIBUTING.md](CONTRIBUTING.md). CI runs the linter, the
tests and a size/complexity budget (`tools/quality_budget.py`): no function may exceed complexity 15 or 80
lines, and the committed baseline is empty. When a real run shows an output baton did not expect, add it as a
fixture with a test. Releases: [docs/releasing.md](docs/releasing.md). Security: [SECURITY.md](SECURITY.md).
The whole documentation is indexed in [docs/README.md](docs/README.md).
