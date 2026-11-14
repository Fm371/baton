# The environment: sandbox, services, notifications, paths

## Sandbox limits, services, notifications

Three things that decide whether a night works have nothing to do with the code being written.

### Env-limited results

Codex's `workspace-write` sandbox has no network and refuses `socketpair`, so a test suite that uses
Starlette's `TestClient`, a localhost database or an HTTP call fails inside it with `PermissionError:
[Errno 1] Operation not permitted`. The task then ended `partial` ("the full suite is not green") and
everything after it inherited the doubt, although the code was fine.

When a `partial` or `blocked` attempt's output (or result file) shows such a restriction, baton records it as
**env-limited**: the task note starts with `env-limited: <what was blocked>`, SUMMARY shows `partial
(env-limited)` or `blocked:env`, and `status.json` has `"reason": "env"`. A `blocked` verdict that is not
environmental shows as `blocked:finding`. A `done` task is never marked. Only specific signatures count
(a permission error after a socket call, a blocked connect, a bwrap failure); a generic failure never does.
The same label is used when a service the task `needs` (below) is down once the attempt ends.

```toml
[run]
env_limited_action = "note"                # default: only label it
# env_limited_action = "retry_other_provider"   # give the task one more attempt on the other provider
```

`retry_other_provider` runs once per task, never with `lanes > 1`, and only when another enabled provider is
available and has a model for the task's rung. The new attempt is told that the previous verdict came from a
sandbox limit and which checks could not run.

To let Codex reach a local database or API, set `[providers.codex] network = true` (it adds
`-c sandbox_workspace_write.network_access=true`; Codex only, and it needs a restart of the run because
`baton.toml` is read at start). List the endpoints that must work in `[preflight] services_tcp =
["127.0.0.1:27017"]`: `baton plan` connects from this machine, and, when Codex's sandbox works, from inside it
(`codex sandbox -- ...`), and **fails** with the fix when the sandbox cannot reach it.

### Services

```toml
[services.mongo]
check = "mongosh --port 27017 --quiet --eval 'rs.status().ok'"   # exit 0 = healthy
restart = "docker start app-test-db"                          # optional, tried once
endpoint = "127.0.0.1:27017"        # optional: told to the agents so they do not look for another instance
timeout_s = 30                      # per check
wait_s = 5                          # pause after a restart, before the second check
```

and in a task file `needs = ["mongo"]` (or `[run] services_for_all_tasks = true`). baton checks the service in
`baton plan`, before every task that needs it (including a `repo-suite`), runs `restart` once if the check
fails, and if the service is still down **stops the run** with the reason before the task starts: no attempt
is spent against a dead database, and the task stays unstarted. Fix the service and run baton again. The prompt
of a task that needs services says which ones baton just verified and where they are. `check` and
`restart` are strings (run through `/bin/sh`) or argument lists; they come from `baton.toml` only, never from
a task file or an agent.

A `plan` with a service down is an error when there is no `restart`, a warning when there is one.

### Notifications

```toml
[run]
on_event = "ntfy publish my-baton"      # or a list: ["curl", "-s", "-d", "baton", "https://..."]
on_event_sleep_min = 30                 # a wait for a reset at least this long is an event
on_event_timeout_seconds = 15
```

The command is run (never through a shell) for these events, with the facts in environment variables
`BATON_EVENT`, `BATON_TASK`, `BATON_STATUS`, `BATON_NOTE` (one line) and `BATON_RUN_ID`:

| `BATON_EVENT` | when |
|---|---|
| `task_done`, `task_partial`, `task_failed`, `task_blocked` | a task ends (status in `BATON_STATUS`, e.g. `blocked:env`) |
| `sleep` | the run waits for a reset longer than `on_event_sleep_min` |
| `run_end` | the run finished its queue |
| `run_stopped` | the run was stopped: Ctrl+C, SIGTERM/SIGHUP, an emergency brake, a service that stayed down. (`baton stop` ends the run cleanly and sends `run_end`.) |
| `run_died` | baton crashed with an exception, or the **next** run found work left mid-way |

A hook that fails, hangs or is missing is logged and ignored; it never changes the outcome of the run. A baton
killed with SIGKILL, a power cut or a closed lid cannot report itself: the next `baton run` sends `run_died`
for the work it finds left mid-way. A notification that arrives only when the run ends well is not enough
for a night; watch for the absence of `run_end` too.

## Checks before a task starts: environment, paths and the sandbox

**Required environment variables.** A verifier started from a new shell without its database URL
does not fail: its database tests *skip*, and a minutes-long, expensive run proves nothing.
List what a task needs and baton refuses to spend an attempt without it:

```toml
[run]
required_env = ["APP_TEST_DB_URI"]          # every agent task

[providers.claude]
required_env = ["ANTHROPIC_FOO"]                # only when this provider runs a task
env = { APP_TEST_DB_URI = "mongodb://127.0.0.1:27017" }   # a value can come from here instead of the shell
```

and, per task, `required_env = ["OTHER_VAR"]` in the front matter. `baton plan` reports a missing
variable as an error; the run checks again when the task starts (and again for the provider it
picks) and stops with exit code 3 before any attempt. A variable counts as set when the shell that
runs baton exports it or the provider's `env` table sets it. A result that still reports tests
**skipped because a variable is missing** is `blocked` with `TaskState.reason = "env"` for a
verifier (a verifier cannot be `done` on skipped tests), and keeps its `partial`/`blocked` status with
a `blocked:env` note for any other task. A task that ended `done` is left alone.

**Paths, and moving the baton folder.** Absolute paths break silently when a baton folder moves
to another name, machine or user: in `common.md`, in task files, and in the allow/deny rules of
`claude-settings.json` (a deny rule for `/old/path/.env` just stops protecting anything). Write
`{base_dir}` instead; baton replaces it with the folder of `baton.toml` in `common.md`, task bodies,
the `repo` line of a task file and the Claude settings file (a per-attempt copy is written, your file
is not changed). `baton plan` warns about every remaining absolute path that does not exist, and about a
settings rule that lets the agent write outside the baton folder and the task repositories. Paths that
start at a folder that does not exist on this machine (`/api/plans` in a task text) are URL paths and are ignored.
After moving a folder: fix `repo` paths in `baton.toml` and task files, recreate virtual environments
inside the repositories, and read the `plan` warnings.

**The Codex sandbox and `socketpair` / localhost.** A sandbox that starts can still refuse what
test suites need: `socket.socketpair()` (asyncio, Starlette's TestClient) and a connection to
`127.0.0.1` (a test database). The suite stalls, the task ends `partial`, and the same test passes in
a fraction of a second on your machine. `baton plan` runs a tiny script directly and inside
`codex sandbox --` and warns when the sandbox refuses something the host allows. The fix is
`[providers.codex] network = true`, which adds `-c sandbox_workspace_write.network_access=true`.
That **opens the whole internet** to the agent, not only localhost; the alternative is to keep such tests
out of Codex (a verifier on Claude, or `repo-suite`, which baton runs itself outside the sandbox).

**`current` in the saved state.** `current` (the provider a mid-attempt task continues on) is kept
in `state/run.json` only while a task is mid-attempt and cleared otherwise, so a normal start no longer
logs "state says current = X, but no task is mid-attempt".
