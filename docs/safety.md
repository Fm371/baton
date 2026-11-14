# Safety

What baton enforces itself, what it leaves to the tools, and how to run it safely.

- Only the official `claude` and `codex` binaries, with your own login, one
  account each. No internal usage endpoints, no credential handling, no
  account rotation.
- **baton itself** never pushes, resets, cleans, rebases or stashes. That is a promise about
  baton's own git calls; it says nothing about what an agent does. The prompt goes in on stdin
  (`--add-dir` takes several values and would swallow a trailing prompt).
- **The agent is limited only by the tool's own settings or sandbox.** Claude gets
  `--settings` with allow/deny rules (`Edit(path)` deny rules cover every file-writing tool);
  Codex gets `--sandbox`. baton does not add a sandbox of its own. Anything the settings allow
  runs unattended with your user's rights, so:
  - Run baton as a **separate user, or in a container/VM** that holds no credentials you would
    mind losing (no SSH keys, cloud logins or tokens it does not need), and give it only the
    repositories it should touch.
  - Treat `claude-settings.json` as required. The example allows file tools, read-only shell
    tools, `git add`/`git commit`, and common test, lint and typecheck runners; it denies git history rewriting,
    `push`, network tools, `rm -r`, secrets (`.env*`, keys, `~/.ssh`, `~/.aws`) and CI files.
    Rules match a command by prefix, so an interpreter such as `python3 -c`, `node -e`, `find -exec`
    or `bash -c` can reach around any deny rule: the example denies those instead of allowing
    them. A test runner (`pytest`, `npm test`, `make test`) still runs whatever code is in the
    repository, which is the price of letting the agent run tests; drop those lines if you
    cannot accept it, or run baton in a container.
  - Give each task a narrow `repo` and `paths`, set `branch` explicitly, and use `providers = [...]`,
    a low `max_turns` and `timeout_min` for risky tasks.
- **`results/` is writable by every agent.** baton adds it to Claude's `--add-dir` so the agent can write
  `results/<id>.md`, which means a task can, in principle, read or overwrite another task's result and
  progress files (verifiers are the exception: they are denied `results/` and `tasks/`). baton decides
  a task's final status from its own result file and commits, but a misbehaving agent could still
  confuse a later task's hand-off notes. Another reason to run it as a separate user or in a container.
- Under `workspace-write` Codex may not be able to write `.git`; baton then commits its work as
  WIP. To run Codex inside a container, point `providers.codex.bin` at your wrapper and set
  `sandbox = ""`.
- `baton plan` refuses a dirty repo (except the one a crashed task left,
  which is committed as WIP).

## The reviewer is read-only

`baton review` runs the other provider on finished work with write access removed, as far as each CLI
allows: Claude gets deny rules (no edits to the repository or the baton folder except `reviews/`, no
file- or repository-changing commands; `verifiers/` stays hidden) and Codex gets `--sandbox read-only` with
no network. It can still *read* everything its CLI can read; Codex has no read restriction, so as for any
Codex run the protection of `verifiers/` is that its path is never in the prompt. Command-prefix rules are a
best effort, so baton compares the repository before and after and discards a review if anything changed.
`baton review-fix` is a normal write run of the implementing provider, with the same settings as its
earlier attempts. Details: [operations.md](operations.md#cross-provider-review).

## What baton checks for secrets

- **WIP commits** (`chore(<task>): wip, ...`) use a two-tier filter on file names: files like `.env`,
  `*.pem` and SSH keys are **never** committed (an untracked one goes to `.git/info/exclude`); names that are
  often ordinary code or data (`*.key`, `credentials*`, `secrets.*`, `.npmrc`) **are** committed, with a loud
  warning in the log, the task note and `SUMMARY.md`. Both lists are yours to change
  (`wip_secret_patterns`, `wip_warn_patterns`); the trade-off and a pre-push scan suggestion are in
  [tasks.md](tasks.md#branches). The filter only covers WIP commits baton makes itself, and only new or
  untracked files: it cannot keep a *tracked* secret file out of a commit, and it does not read file contents
  beyond the private-key header sniff.
- **Hardcoded data keys and forbidden literals** in added lines are scanned once per task
  ([verifiers.md](verifiers.md#hardcoded-keys-and-error-messages)).
- baton never prints the values of environment files: env-preflight reads key names only, and credentials in a
  database URL are never printed.

## Before the first real night

1. Run baton as a separate user, or in a container or VM, with only the repositories it should touch.
2. Read your `claude-settings.json`; deny the paths a task must never edit.
3. Give each task a narrow `repo`, an explicit `branch`, and a low `max_turns` and `timeout_min` when it is risky.
4. Start with `lanes = 1`, two or three small tasks, and read the branches in the morning before you merge.
5. Set `[run] on_event` so something tells you when the run dies ([environment.md](environment.md)).
