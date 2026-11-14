# Troubleshooting

**Every Codex task fails with a `bwrap` error** (`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`,
`No permissions to create new namespace`). Codex's sandbox needs unprivileged user namespaces. Two causes:

1. You started baton from inside another agent's sandbox. Start it from a plain terminal.
2. Ubuntu 24.04 and later restrict user namespaces with AppArmor. The quick fix is global and lost at
   reboot:

   ```bash
   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
   ```

   Keep it across reboots, again globally, with a file in `/etc/sysctl.d/`. The narrower fix is a
   profile that lets only `bwrap` create namespaces (adjust the path to `which bwrap`):

   ```bash
   sudo tee /etc/apparmor.d/bwrap-userns <<'EOF'
   abi <abi/4.0>,
   include <tunables/global>
   profile bwrap /usr/bin/bwrap flags=(unconfined) {
     userns,
   }
   EOF
   sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns
   ```

   If Codex starts its own `bwrap` copy (for example from a bundled path), add a profile for that
   binary the same way. `baton plan` runs a sandboxed no-op and prints this hint when it fails; Codex is
   left out of the run (the work goes to Claude) instead of failing every task.

**Codex cannot reach the network (a database, a local server) and tests hang or skip.** The
`workspace-write` sandbox blocks the network and `socketpair`. Set `[providers.codex] network = true`
(this opens the whole internet to the agent, not only localhost), list the endpoints that must work in
`[preflight] services_tcp`, restart baton (the config is read once at start) and check that `baton plan`
shows no sandbox warning. See [environment.md](environment.md). Tests that need a real service should fail
loudly when it is down, not pass on mocks: declare it under `[services.<name>]` and `needs = [...]`.

**A test fails in one task and every later task ends `partial` ("the suite is not green").**
Tell the tasks to judge only *new* failures: run the suite at the start, note the failing test ids, and
report only tests that fail now and did not before. `repo-suite` with `suite_baseline` does this for the
whole feature (see [verifiers.md](verifiers.md#repo-suite)).

**`config file not found`**: baton looks for `baton.toml` in the current folder and the folders above it.
Run it inside the workspace, or set `BATON_WORKSPACE=/path/to/task-auto`, or pass `-c PATH`.

**`another baton run is already active in this folder`** with no run in sight: the earlier process may
be asleep in tmux (`tmux ls`, `baton status`). `baton stop` asks it to finish.

## `baton plan` refuses to start

**`PREFLIGHT FAILED: <repo> has uncommitted changes; commit them first`.** baton starts only on a clean
working tree (except the one a crashed task left, which it commits as WIP). The message lists the first few
files. If they are baton's own `results/`, `state/` or `logs/`, the workspace is inside the repository: move it
out (see [quickstart.md](quickstart.md)) or add those three folders to `.gitignore` or `.git/info/exclude`.

**`<path> is not a git repository (the repo path in the task file is relative to baton.toml's folder)`.** A
`repo` line is read relative to the folder of `baton.toml`, not to where you stand. A bare repository is
refused as well.

**`base <name> does not exist in <repo>`** or **`branch <name> does not exist and no base is set`.** baton
never fetches or creates the base. Create it yourself (`git branch develop origin/develop`) or set
`default_base`/`base` to a branch that exists.

**`the agent's commits would fail; set it with git config user.name ...`.** The repository has no git
identity; baton makes commits for the agent and for WIP. Set `user.name` and `user.email` in the repository.

**`'<branch>' is not a valid branch name`.** Git refuses the name (two dots, a trailing `.lock`, a space).
When it comes from the task id (the file name), rename the file.

**`unknown key '<name>' (did you mean '<other>'?)` in `baton.toml`.** Unknown keys are errors, not ignored.
Fix the spelling or remove the key; every valid key is in [configuration.md](configuration.md).

**`required_env`: a variable is missing.** `baton plan` names it. Export it in the shell that runs baton, or set
it under `[providers.<name>.env]`; baton does not start an attempt (and the verifier does not "pass" on skipped
tests) without it.

**A service is down** (`[services.<name>]`). `baton plan` runs the `check`; with a `restart` it is a warning,
without one an error. During a run baton tries `restart` once, then stops with exit code 3 and
`waiting for the environment`, before spending an attempt. Fix the service and run `baton run` again.

## During a run

**A task is refused: its branch exists but does not contain the work it depends on.** An old `feat/*` branch
from an earlier night lacks the dependency's commits (or shares no history with the base). Delete or rename the
old branch, or set `existing_branch = "rebase"` (rebases it onto the dependency; a conflict refuses the task)
or `"warn"` (uses it anyway). See [tasks.md](tasks.md#branches).

**`cannot prepare repository: <repo> has uncommitted changes; cannot switch to <branch>`.** Something left the
repository dirty (a file baton would not commit, or you were working in it). Commit or stash it yourself; the
log and the task note list the files. Files that look like secrets are never committed in a WIP commit, so
they stay behind by design ([tasks.md](tasks.md#branches)).

**`no limit pattern matched; if this was a usage limit, the message format may have changed`.** An attempt
failed and baton could not tell why. It waits the cooldown (`unknown_limit_cooldown_min`) and keeps the raw
output in `state/fixtures/`. Send that file, or add it to `tests/fixtures/limits/`
([development.md](development.md)). `baton providers reset claude` clears a limit baton could not read the end
of, once you know the provider is available.

**A task ends `partial (env-limited)` or `blocked:env`.** The environment, not the code, stopped it (a sandbox
that refuses sockets, a service that was down, a variable that was unset). See
[environment.md](environment.md). After fixing the cause: `baton retry <id>`.

**A provider is alive but output stopped, including after suspend/wake.** Read `baton status` or
`baton watch` for last output, inactivity and the last meaningful stdout/stderr activity, then run
`baton resume TASK_ID`. The owner replaces only that attempt with a fresh session and preserved work/notes;
when no owner is active, the same command resumes the saved task branch under the normal lock. Do not
send manual signals or switch branches while an owner is still cleaning up. Sleep is a possible cause,
not a diagnosis. Idle follow loops and provider cooldown/approval waits have no child to interrupt.

**Resume says control is unacknowledged, cleanup is pending, or the owner lacks resume control.**
The command refuses to start another state writer while the owner holds its lock. Check `logs/run.log`
and `baton status`. An old runner cannot acquire this feature mid-run: after its normal shutdown and
lock release, rerun `baton resume TASK_ID`. No lock bypass or `--force` is supported. If leftover process
identity cannot be established, resume reports the recorded group and refuses to signal it or start work;
inspect that process identity before further recovery.

**Resume refuses a scaffold-to-task branch transition.** It allows untracked dependency output only when
the saved task branch ignores every untracked path, and refuses tracked edits and file collisions with the
branch and paths. All files stay intact. Resolve those named conflicts and rerun resume. A normal ignore
transition needs no manual branch switch and never commits dependencies as WIP on the scaffold base.

**A task ends `failed` after "stalled".** Its output did not grow for `stall_minutes` twice. Inspect both
`results/<id>/attempt-NN.<provider>.out` and `.err`. The watchdog defaults to off; enable it explicitly for
the workspace or a task, and use task `stall_minutes = 0` for legitimate quiet commands. Possible suspend
observation gaps grant a fresh interval, rather than repeatedly spending retries. `baton resume ID` keeps
history and the spent watchdog budget; `baton retry ID` starts over after you resolve the cause.

**`another baton run is already active in this folder (pid N, since ...)`.** Look at `baton status` and
`tmux ls`; the process may be asleep. `baton stop` asks it to finish. The lock is released by the system when
the process dies, so a crashed run never leaves a lock to clean up.

**`disk full in <folder>`** (exit code 6). baton stopped and wrote nothing more; `state/run.json` is its last
good save. Free space (old `results/<id>/attempt-*.out` and `logs/` are safe to delete, see
[operations.md](operations.md#housekeeping)) and run `baton run` again.

**`run.json` is damaged.** baton falls back to `state/run.json.bak` and says so. With no backup it exits with
code 2 and a recovery hint; do not delete `state/` blindly, it holds the decisions and the fixtures.

**A task ended `done` or `partial` but you think it is wrong, or it is stuck behind a gate.** `baton status`
ends with "what to do next". `baton mark <id> done|failed|blocked --note "why"` records your decision;
`baton retry <id>` clears one task and runs it again. Never edit `state/run.json`.
