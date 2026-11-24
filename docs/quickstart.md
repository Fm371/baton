# Quick start

From nothing to a first successful run in about ten minutes, on a throwaway repository, using the real
`claude` (and optionally `codex`). You need Python 3.11 or newer; baton has no runtime dependencies.

## 1. Install

The package is named `baton-runner`; the command is `baton`.

```bash
pipx install baton-runner               # or: pip install --user baton-runner
baton --version
```

The latest unreleased code: `pipx install git+https://github.com/Fm371/baton`. To hack on baton itself, clone
it and use `pip install --user -e .` (see [development.md](development.md)).

You also need the tools baton drives, installed and **logged in** as the user that will run baton:

- `claude` (Claude Code), and `codex` if you want the switch between the two. With only one of them, set
  `enabled = false` on the other provider in `baton.toml`.
- `git`.

## 2. Create a workspace

A workspace is a folder holding `baton.toml`, a rules file, your tasks, and (once you run) the results.
Create one with the template:

```bash
baton init ~/work/task-auto
cd ~/work/task-auto
ls
```

`baton init` copies the template (config, `common.md`, a sample task and `claude-settings.example.json`) into
the folder. If the workspace has no `claude-settings.json` yet, copy
the example and read it: that file is what limits what the Claude agent may run (see [safety.md](safety.md)).

```bash
cp claude-settings.example.json claude-settings.json
```

Keep the workspace **outside** the repositories it works on. Its `results/`, `state/` and `logs/` would
otherwise show up as uncommitted changes in them, and `baton plan` refuses a dirty repository.

## 3. Make a throwaway repository

```bash
mkdir ~/work/toy && cd ~/work/toy
git init -b main
git config user.name "you" && git config user.email "you@example.com"   # if you have no global identity
echo "# toy" > README.md
git add -A && git commit -m "init"
```

baton needs a git identity in each repository (it makes commits for the agent), and it needs the **base
branch** to exist locally. It never runs `git fetch` or `git pull`: a task branches from the *local* base,
so update that branch yourself before a real night.

## 4. Point baton at it

Edit `baton.toml`:

- `default_base = "main"` under `[run]` (the branch new task branches start from).
- Leave `timezone` out unless you know the CLI prints another zone: baton uses this machine's.
- Check the model names under `[routing.rungs.*]` against what your `claude` and `codex` offer. `baton plan`
  checks the Codex ones.

Then replace the sample task with a real one, `tasks/10-hello.md`:

```markdown
+++
repo = "../toy"           # relative to baton.toml's folder, or absolute
work_type = "docs"        # a key of [routing.work_types] in baton.toml
+++
## Goal
Create HELLO.md at the repository root with one line: `hello from baton`.

## Acceptance
- HELLO.md exists and contains exactly that line.
- The change is committed.
```

(Delete the sample task file the template made, or `baton plan` will try to run it too.)

## 5. Plan, then run

```bash
baton plan
```

`baton plan` checks everything it can (the tools are on `PATH`, repositories are clean, base branches exist,
git identity, model ids, the Codex sandbox, config warnings), prints the order and the model each task would
use, and **creates nothing on disk**. Fix every `PREFLIGHT FAILED` line, read every warning, then:

```bash
baton run
```

Watch it from another terminal:

```bash
baton watch       # live view of the agent's output
baton status      # the summary table and what to do next
```

## 6. Read the result

- The task worked on a branch called `baton/10-hello` in `~/work/toy`; after the run the repository is back
  on the branch it was on (`main`). `git -C ~/work/toy log baton/10-hello` shows the agent's commit.
- `results/10-hello.md` is the agent's own report; its first line is `STATUS: done`, `partial` or `blocked`.
  baton decides the final status itself from that line and the commits (see [tasks.md](tasks.md)).
- `results/SUMMARY.md` is the table of every task, with notes, branches and anything you must do by hand
  ("Needs from you").
- `logs/run.log` says what happened and why (`switch`, `sleeping until`, `ERROR ...`).

baton never merges and never pushes. Review the branch and merge it yourself.

## Next

- Several tasks, dependencies, branches, human checkpoints: [tasks.md](tasks.md).
- An independent check of the agent's work: [verifiers.md](verifiers.md).
- Before the first real night: [operations.md](operations.md) (the checklist) and [safety.md](safety.md).
- Something failed: [troubleshooting.md](troubleshooting.md).
