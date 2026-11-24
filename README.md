# baton

**An unattended task runner for Claude Code and Codex. Queue coding-agent tasks as Markdown files, go to
sleep, review git branches in the morning.**

[![CI](https://github.com/fm371/baton/actions/workflows/ci.yml/badge.svg)](https://github.com/fm371/baton/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

When an account hits its usage limit, baton passes the task to the next available account with the previous
agent's notes, or sleeps until a limit resets. Each task gets its own git branch. baton never pushes and never merges:
you decide what lands.

This is what you find in `results/SUMMARY.md` after a night (shortened example):

| task | repo | status | commits | attempts | providers | note |
|---|---|---|---|---|---|---|
| 05-schema | api | done | 2 | 1 | codex | - |
| 10-widgets | api | done | 4 | 2 | codex -> claude | - |
| 15-check-widgets (verify) | api | done | 1 | 1 | claude | - |
| 20-export | api | partial | 3 | 1 | claude | the export tests need a database that is not running |

Task `10-widgets` started on Codex, Codex ran out of quota, and Claude Code finished it.

## Why baton

Long agent runs fail in boring ways. baton handles each of them without you:

| what goes wrong at 2 a.m. | what baton does |
|---|---|
| The usage limit hits | Reads the limit message, moves the task to the other tool on the same model tier, or sleeps until the real reset time |
| A half-finished change is left uncommitted | Commits it as a WIP commit (never a stash) and hands the next agent the notes |
| The tests are green but prove the wrong thing | Runs an independent verifier that never sees how the code was written, and holds back the tasks that depend on it |
| One task breaks and takes the night with it | Marks that task failed or blocked, carries on with the rest, and tells you why in the summary |

## Quick start

You need Python 3.11+, `git`, and `claude` (Claude Code) and/or `codex`, installed and logged in.
baton itself has no dependencies.

**1. Install**

```bash
pipx install baton-runner     # the command is `baton`
```

**2. Create a workspace and a throwaway repository to try it on**

```bash
baton init ~/work/baton-demo && cd ~/work/baton-demo
cp claude-settings.example.json claude-settings.json     # what the Claude agent may run: read it
sed -i 's/default_base = "develop"/default_base = "main"/' baton.toml

git init -b main ../toy && git -C ../toy commit --allow-empty -m init

rm tasks/10-example.md
cat > tasks/10-hello.md <<'EOF'
+++
repo = "../toy"
work_type = "docs"
+++
## Goal
Create HELLO.md at the repository root with one line: `hello from baton`.

## Acceptance
- HELLO.md exists, contains exactly that line, and the change is committed.
EOF
```

**3. Check, then run**

```bash
baton plan       # checks tools, repositories and config; shows the order; changes nothing
baton run        # runs every unfinished task
baton status     # the summary, and what to do next
```

The work is on the branch `baton/10-hello` in `../toy`, waiting for your review. If you have only one of the
two tools, set `enabled = false` on the other provider in `baton.toml`. The step-by-step version, with
explanations, is in [docs/quickstart.md](docs/quickstart.md).

## A task is a Markdown file

```markdown
+++
repo = "../my-repo"           # relative to baton.toml, or absolute
work_type = "crud"            # picks the model on each tool, from a table in baton.toml
depends_on = ["05-schema"]    # optional
+++
## Goal
Add the widgets list endpoint.

## Acceptance
- GET /widgets returns the user's widgets, newest first; tests cover empty and paged results.
```

The agent works on the branch `baton/<task id>` and writes `results/<id>.md`, whose first line is
`STATUS: done|partial|blocked`. baton decides the final status from that line and the commits.
Everything a task header can hold is in [docs/tasks.md](docs/tasks.md).

## How it works

**Switching between accounts.**

1. Start on `start_provider` (Codex by default) and stay on it until it is limited.
2. When it is limited, continue on the next available account. New tasks prefer the highest-priority
   account available to them.
3. When every eligible account is limited, sleep until the earliest reset.
4. A task that switches accounts keeps its model tier and gets the previous agent's notes.

You can configure several logins for either CLI and give them a fallback order. See the
[Business-to-Personal Codex example](docs/configuration.md#codex-business-then-personal).

**Independent verifiers.** A verifier is a task in `verifiers/` with `kind = "verify"`. It runs in a fresh
session, cannot read the implementer's task or result files, and may only add or change tests. Until it
finishes `done`, every task that depends on it is held back. The implementer never sees the verifier's
text. See [docs/verifiers.md](docs/verifiers.md).

**Human checkpoints and second opinions.** A task can wait for `baton approve ID`, and `baton review` asks
the other tool for a read-only review of a finished task. See [docs/tasks.md](docs/tasks.md) and
[docs/operations.md](docs/operations.md).

## Commands

| command | what it does |
|---|---|
| `baton init` | create a workspace from the template |
| `baton plan` | preflight: check everything, print the order and models, run nothing |
| `baton run` | run every unfinished task (`--only ID` for one, `--follow` to keep picking up new tasks) |
| `baton status` / `baton watch` | the summary table / a live view of the current agent's output |
| `baton stop` | from another terminal: finish the current task, then end cleanly |
| `baton resume ID` | replace a stalled or unfinished attempt with a fresh session that keeps the work and notes |

The rest (`approve`, `reject`, `mark`, `retry`, `review`, `review-fix`, exit codes, the night checklist) is in
[docs/operations.md](docs/operations.md).

## Safety: read this before a real night

baton does not push, merge, reset, clean or stash. It does not commit on your current branch, and it puts
every repository back where it was when the run ends. It never runs `git fetch`: update your base branch
first.

**baton does not sandbox the agent.** The agent runs unattended with your user's rights, limited only by the
allow/deny rules in your `claude-settings.json` and by Codex's own sandbox. Run baton as a separate user or
in a container, and read [docs/safety.md](docs/safety.md) first.

## Is it for you?

It fits when you have a list of well-specified tasks, a subscription to Claude Code, Codex or both, and you
would rather review branches than babysit a terminal. It does not fit interactive pair programming, vague
tasks that need a conversation, or Windows.

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
| [docs/limitations.md](docs/limitations.md) | everything baton does not do yet |

## Status and known limitations

Version 0.4.0, pre-1.0, in continuous use on real projects with the real `claude` and `codex` tools.
Changes are in the [CHANGELOG](CHANGELOG.md).

- **No sandbox of its own** (see [Safety](#safety-read-this-before-a-real-night)).
- **Linux is the tested platform.** macOS should work but is untested. Windows is unsupported.
- **One run per workspace.** A second `baton run` in the same folder refuses to start.
- **Parallel lanes and `return_mid_task` are experimental.** Try them on a low-risk night first.

The full list is in [docs/limitations.md](docs/limitations.md).

## Contributing

The tests are plain `unittest` with no dependencies: `python3 -m unittest discover -s tests -t .` See [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/development.md](docs/development.md). When a real run shows an output baton did not expect, please
send it as a fixture with a test. Security reports: [SECURITY.md](SECURITY.md). Releases:
[docs/releasing.md](docs/releasing.md). MIT licensed.
