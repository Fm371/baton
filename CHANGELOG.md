# Changelog

## 0.4.0 — not yet released

### Fixes from the field report (25 issues seen on a real workspace)

- **Codex "out of credits" is a limit, not an auth error.** `classify_codex` reports it as a `limit` with the
  reset from Codex's own rate-limit event (else its rollout, else an hourly retry), so Codex comes back
  after the reset instead of being disabled for the whole run. Claude's out-of-credits stays `auth`.
- **The Codex sandbox probe uses the provider's settings** (`sandbox`, and `network = true`), so it no longer
  reports a refused TCP connection that real tasks do not have.
- **`after = [...]` task key:** run order only. No gate, no skip when the other task failed, no branch
  containment check, no base; works across branches of one repository.
- **Held-back tasks are released when their dependency finishes** (a retried verifier, a dependency fixed
  by `baton retry`, a checkpoint skipped earlier in the run), in `--follow` runs too, without a restart.
- **`--follow` reads task files before each pick.** Edited `depends_on` / `after` of pending tasks take
  effect at once and are logged; an awaiting checkpoint whose dependencies are no longer met goes back to
  pending, and `baton approve` refuses while a dependency is open.
- **`baton retry` during a run is queued into that run** (like `mark` and `approve`).
- **`baton mark <id> partial`** retires a task (a superseded verifier) without rerunning it.
- **A resumed verifier's test-path check starts from the current head** when other tasks committed on its
  branch meanwhile.
- **`run.allow_codex_verifiers`:** accept Codex as a verifier provider with a warning; finished verifiers are
  no longer refused by preflight.
- **`run.restore_branches = false`** leaves repositories on the last task branch; the log says what was
  switched back.
- **Claude's commit trailer is switched off** in the settings baton writes (`includeCoAuthoredBy`,
  `attribution`), which is now written for every Claude attempt.
- **Per-task `settings` and `sandbox`** (Claude settings file, Codex sandbox mode) and **`creates_base`**
  (no main-line warning for a task that creates the default base).
- **Plan warnings:** `Bash(path:*)` rules lose their wildcard before the path check, relative rules are
  skipped, allow/deny/dir get their own text, `{base_dir}` is suggested only when the file exists under the
  baton folder, paths named by an earlier task are not reported, and a missing `results/` in `add_dirs` is
  created instead of refused. A dependency through an approval checkpoint keeps its branch base.
- **The hardcoded-key scan counts only commits made since the task started**, not history it imported.

Not changed: #24 (compound read-only commands asked for approval in non-interactive runs) depends on the
Claude settings of the workspace; allow the forms you need under `permissions.allow` in
`providers.claude.settings` (or per task with `settings`). `rerun_after` for verifiers (#14) is not
implemented; `after` provides the ordering.


- **Expired limits are dropped when the state is loaded.** A provider limit (or per-model limit) whose reset
  time has passed is cleared as `run.json` is read (`Runner`, `baton plan`, `baton status`), with one log
  line, so `SUMMARY.md` and `baton plan` never show `limited until <a past time>`. A disabled provider
  stays disabled.
- **`return_mid_task` per task.** `return_mid_task = false` (or `true`) in a task's front matter overrides
  `[run] return_mid_task` for that task; `baton plan` shows `return: off`. A quoted value is a `TaskError`.
- **Two-tier, configurable WIP secret filter (`[run] wip_secret_patterns`, `[run] wip_warn_patterns`).**
  The refuse tier keeps its default (`.env*`, `*.pem`, `id_rsa*`, ...) and its behaviour (never committed,
  untracked files added to `.git/info/exclude`). The warn tier (`*.key`, `credentials*`, `secrets.*`,
  `.npmrc`, `*.token`, `*.secret`, `service-account*.json`, `.netrc`, `.htpasswd`) is still committed so
  the queue never blocks, but each file now gets a log warning, a task note, a `## Warnings` block at the
  top of `SUMMARY.md` and the note in the `on_event` payload. Both lists are strictly validated; `[]`
  switches a tier off. Patterns with a `/` match the path from the repository's top folder.

- **Cross-provider review and a single-round review-fix (opt-in, never run by `baton run`).**
  - New task header field `review = true|false` (default `false`; a quoted value is a `TaskError`).
  - `baton review [--only ID | --pending] [--allow-same]`: the provider that did not implement a done task
    reviews it, read-only, from its Goal and Acceptance, `results/<id>.md` and the branch diff (never
    `verifiers/`). Output `reviews/<id>.md`: first line `VERDICT: approve|changes|reject`, findings tagged
    `[blocker]`, `[should]`, `[nit]` with `path:line`. The implementer is recorded in `run.json`
    (`implemented_by`) when a task finishes. If the other provider is limited or unavailable the command fails
    with the reason; it falls back to the implementer only with `--allow-same`. A review never gates other
    tasks. Read-only: Claude gets deny rules (repository and baton folder except `reviews/`, file- and
    repository-changing commands) and Codex `--sandbox read-only`; the working tree, `HEAD`, refs and stash
    are compared before and after, and a review that changed anything is discarded and marked `blocked`.
  - `baton review-fix --only ID [--force]`: one round, by the implementing provider only (a limit is waited
    out like for a normal task, or the command exits blocked), for the review's `[blocker]` findings
    (`[should]` optional, `[nit]` ignored, Goal scope only, declining with a reason allowed). Commits on the
    task branch, never pushes, appends a `## Review fix` section to `results/<id>.md`. Refused without a
    review, without blockers, or when `fix_round` is already 1 unless `--force`; `fix_round` is recorded
    before the agent starts. No automatic re-review; a manual `baton review` never reopens the round.
  - `baton plan` and `baton status` show the review state (`none|pending|approve|changes|reject`) and the
    fix state (`not run|done|blocked`) per task. `run.json` gains `implemented_by`, `review_verdict`,
    `review_by`, `review_at`, `fix_round`, `fix_state` per task (old files still load). Both commands take
    the run lock (exit 4 while a run is active); exit 3 means blocked.

**Behaviour changes in this release** (read before upgrading from 0.3.0): unknown keys in `baton.toml`
(top level, `[run]`, `[providers.*]`) stop baton with an error; a task without `branch` works on
`baton/<id>` instead of committing on the current branch, and every repository is put back on its
original branch at the end; WIP commits skip files that look like secrets; the example
`claude-settings.example.json` is much stricter (it no longer allows `python3`, `node`, `npx`,
`find`, `cat`); `return_mid_task` is **off by default** (see below).

- **Packaging and release.**
  - `baton init [DIR]` writes a ready-to-edit workspace (`baton.toml`, `common.md`, an example task and
    `claude-settings.example.json`) from templates shipped inside the package, so a pip-installed baton no
    longer needs a clone to start. The templates moved from `examples/task-auto` to
    `baton/templates/task-auto`. A folder that is not empty is refused unless `--force`.
  - Version 0.4.0, MIT `LICENSE`, full `pyproject.toml` metadata (classifiers, URLs, keywords).
  - GitHub Actions CI (ruff, the test suite on Python 3.11–3.13, `python -m build` + `twine check`),
    `tools/quality_budget.py` (a ratchet on function size and complexity), `CONTRIBUTING.md`,
    `SECURITY.md`, `docs/releasing.md`, `MANIFEST.in`.

- **Defaults and docs (behaviour change).** `return_mid_task` now defaults to `false` and is documented
  as experimental: an earlier version of this switch failed in a real run for a cause nobody found, so
  try it on a low-risk night first (`return_mid_task = true`). The example rungs make `gpt-6.1-sol high`
  / `sonnet high` the workhorse and keep `opus high` for verifiers and the hardest work (the `xhigh` rung
  is gone); `baton plan` warns about an `xhigh`/`max` effort or an `astra`-class model. README fixes where
  it disagreed with the code (the emergency brake fails one task before it stops the run, `start_provider`
  defaults to Codex, a WIP commit counts as a commit for `partial`, ids are ASCII only, a detached HEAD is
  put back on the same commit); the task id error says ASCII. New README sections: launch checklist, how to
  tell the night went wrong (exit codes), what a night costs, troubleshooting (permanent AppArmor profile
  for `bwrap`, Codex without network, inherited test failures) and known limitations.

- **Repository layer.**
  - A `repo` that is only a folder inside a bigger repository is handled as the whole repository: the
    secret and big-file checks, `add`, the exclude file and `repo-suite`'s clean-up all use the top-level
    folder (before, a `sub/.env` was committed). `baton plan` says so.
  - The first changed file of `git status` no longer loses its first character (a modified tracked `.env`
    came back as `env` and slipped past the secret check).
  - No WIP commit in the middle of a merge, rebase, cherry-pick or revert, or with unmerged paths: it would
    close the operation with conflict markers. The task note and the log say why; `recover()` does not crash.
  - A repository with a submodule whose own work tree is dirty is not "dirty" for baton and no longer makes
    every later commit fail with "nothing to commit". Work that could not be committed (changes inside a
    submodule, a modified tracked secret file) ends the task `partial`, not `done`, and is named in its note
    once; `baton plan` warns about submodules.
  - File names that are not UTF-8 no longer raise `UnicodeDecodeError`; they are shown with a `\udcXX`
    escape in logs and notes. `git diff --name-only` is read with `-z`, so non-ASCII names are no longer
    returned quoted.
  - A tag or hash spelled like a branch can no longer turn a checkout into a detached HEAD (`branch_exists`
    looks at `refs/heads/`); a checkout that does not end on the branch is an error.
  - Every git command runs with no stdin, `GIT_TERMINAL_PROMPT=0` and a time limit (5 minutes, 30 for a
    commit with hooks) and its process group is killed on expiry, so a hook or prompt that waits for input
    cannot freeze the run. Multi-line git errors are collapsed to one line before they reach a note or
    SUMMARY.

- **Branches that already exist (`[run] existing_branch`).** A task branch left from an earlier night
  used to be checked out as it was, even when it lacked the work of the task it depends on or shared no
  history with its base (the task then ran without its dependency, and `baton plan` still said "from
  <dependency>"). `baton plan` now shows how many commits the branch has over its base and warns when it
  does not contain a dependency's branch; at run time `existing_branch = "fail"` (default) refuses
  the task with the reason, `"warn"` uses it and logs a warning, `"rebase"` rebases it onto the dependency
  (aborted on conflict). A `verify/*` branch that does not contain the branch it checks is covered the
  same way. An orphan branch is never rebased.
- **The user's checkout is put back more carefully.** A detached HEAD is recorded by its commit and checked
  out again at the end (before, the repository stayed on baton's branch and `baton plan` wrongly said
  "left as is"). A branch an agent left a repository on (the "left on another branch" stop) is remembered
  as stray, never recorded as the user's branch by the next run, and the recorded branch survives until it is
  restored.
- **Clearer refusals.** The uncommitted-changes message lists the first few `git status --short` lines and
  names baton's own folders when they sit inside the repository; a missing base says how to create it and
  detects `origin/<base>` (no fetching); a bare repository gets its own message; `baton plan` warns when
  a task's `branch` is `main`, `master`, the default base or the branch checked out now.

- **Resilience (third review).**
  - A bare `401`, `503` or `capacity` in a stack trace or stderr (`cli.js:401:9`, `line 503`) no longer
    reads as an auth or transient error: a status number counts only next to HTTP/status/error wording.
    Error text inside the CLI's own events outranks stderr and plain output lines.
  - `error_max_turns` results no longer feed the agent's own last words into the limit classifier, and
    a task fails after `max_limit_cycles` (6) usage-limit outcomes in a row that changed nothing in its
    repository, instead of bouncing between providers for ever.
  - Agent output is never read whole: the classifier gets the event lines of the first part of a large
    file plus its last 4 MB (a 650 MB output used to take 2.5 GB of memory); `baton watch` starts
    from the last 2 MB; `state/fixtures` copies only the tail.
  - `state/run.json.bak` keeps the previous good save; a damaged `run.json` falls back to it, and with
    no backup baton prints what to do (exit 2) instead of a traceback. Renames are made durable
    (the directory is fsynced).
  - A full disk stops the run cleanly (exit 6, `disk full in <folder>`), writes nothing more and leaves
    `run.json` intact; `baton plan` warns below `[run] min_free_mb` (500).
  - `[run] stall_minutes` (off by default; `stall_minutes` in a task file overrides it) ends an attempt
    whose output has stopped growing, keeps its work as WIP and retries once with the last output line
    in the prompt; a second stall fails the task.
  - The "no limit pattern matched" warning no longer appears for attempts baton itself ended (timeout,
    stall, return to the preferred provider).
  - A wall clock stepped back during an attempt no longer makes the result file look stale: the
    attempt's start is moved back by the step (a backwards step over one second, seen as the difference
    between wall-clock and monotonic elapsed time). The price: a stale result file written within that
    many seconds before the start would pass as fresh.

- **Environment** (from the real runs of three projects).
  - **Env-limited results.** A `partial` or `blocked` attempt whose output shows a sandbox restriction
    (a permission error after a socket call, `socketpair` refused, a blocked connect, a bwrap failure) is
    labelled env-limited, not a code failure: the note starts with `env-limited: ...`, SUMMARY shows
    `partial (env-limited)` / `blocked:env`, `status.json` gets `"reason": "env"`. A blocked verdict that is not
    environmental shows as `blocked:finding`. `[run] env_limited_action = "retry_other_provider"` gives the task
    one more attempt on the other provider (default `"note"`).
  - **Codex network.** `[providers.codex] network = true` adds `-c sandbox_workspace_write.network_access=true`.
    `[preflight] services_tcp = ["host:port"]` makes `baton plan` connect from this machine and, when the Codex
    sandbox works, from inside it, and fail with the fix when the sandbox cannot reach the endpoint.
  - **Services.** `[services.<name>]` (`check`, optional `restart`, `endpoint`, `timeout_s`, `wait_s`) plus
    `needs = [...]` in a task file (or `[run] services_for_all_tasks = true`): checked in `baton plan`, before
    each task that needs it and before a `repo-suite`; one restart on failure; if it stays down the run stops
    with the reason before the task starts, instead of burning an attempt. The prompt names the verified
    services and their endpoints. A verifier that ends blocked/partial while a service it needs is down is
    `blocked:env`.
  - **Notifications.** `[run] on_event` (a command, never run through a shell) is called with `BATON_EVENT`,
    `BATON_TASK`, `BATON_STATUS`, `BATON_NOTE`, `BATON_RUN_ID` for `task_done|partial|failed|blocked`, `sleep`
    (waits of at least `on_event_sleep_min`), `run_end`, `run_stopped` and `run_died` (also sent by the next run
    when it finds work left mid-way). A failing, slow or missing hook is logged and ignored.
  - Design notes, no code: `docs/design/baseline-failures.md`, `fallback-and-budget.md`,
    `protected-paths.md`, `resume-same-session.md`.

- **Operator commands.** `baton mark ID done|failed|blocked --note ... [--result FILE]` sets a task's
  status without editing `run.json` (records `overridden_by = operator`, keeps the agent's own result
  file, sets the tasks it had gated or skipped back to pending). `baton retry ID` clears one task and
  runs only it (old attempt files move to `earlier-N`). `baton providers reset NAME` clears a stale
  limit or disabled flag. While a run is active, `mark` and `providers reset` are queued in
  `state/ops/` and applied by the run between tasks.
- **`baton status`** is rebuilt from `state/run.json` instead of printing a stale SUMMARY.md, ends with
  "what to do next" (the command that unblocks each stuck task), and no longer dies with a
  BrokenPipeError under `| head`. `baton` finds `baton.toml` in a parent folder or through
  `BATON_WORKSPACE`, with an error that says where to run it.
- **Summary.** The `note` column of a partial/blocked/failed task carries the reason from its result
  file; a wrapped bullet in "Not verified" is joined, not cut; times are ISO with the UTC offset and how
  far away they are, and a limit that has passed reads "available"; a task left mid-attempt shows
  `interrupted`; a Branches section, and `results/BRANCHES.md` at the end of a run (conflicts between
  the run's branches via `merge-tree`, suggested merge order); a **Needs from you** block at the
  top, fed by a `## Needs from you` section every prompt now asks for.
- **Lock and version.** The lock file holds pid, start time and run id, and the "already active"
  message says who holds it. The baton version and commit are recorded in `run.json` and on the first
  line of SUMMARY.md; `[run] min_version` makes `baton plan` warn about an old baton.
- **Between attempts** the task's front matter and `baton.toml` (routing, timeouts, retry limits, each
  provider's extra arguments, env, settings) are read again and the changes logged.
- Smaller: `baton plan`'s first command uses the provider the first task can really use; `baton approve`
  says whether a run is active ("picks it up by itself") and, for a checkpoint that is not due, why;
  "all providers limited" is only written when the waiting tasks could use any provider; a task that
  was mid-attempt shows `interrupted` in `plan`.

- **Real-run lessons (second batch).**
  - `verifiers_last` was wrong in the port of PR #12: it dropped explicit gates (T09, which depends on V5,
    could run before V5). An explicit `depends_on` on a verifier is a real gate again, also for `--only`;
    only verifiers that no task depends on move to the end of the run.
  - `required_env = [...]` in `[run]`, in a `[providers.*]` table and in a task file: `baton plan` reports a
    missing variable as an error, the run stops before an attempt when one is gone, and a result that
    reports tests skipped for a missing variable is `blocked:env` (`TaskState.reason = "env"`) instead of
    a green verifier.
  - `{base_dir}` is expanded in `common.md`, task bodies, a task's `repo` and the Claude settings file;
    `baton plan` warns about absolute paths that do not exist and about settings rules that grant writing
    outside the baton folder and the repositories.
  - `baton plan` probes `socketpair` and localhost TCP inside the Codex sandbox and warns with the fix
    (`[providers.codex] network = true`, which opens the internet).
  - The "state says current = X, but no task is mid-attempt" warning is gone: `current` is cleared when no
    task is mid-attempt.

- **Back mid-task and `verifiers_last`** (from PR #12, re-applied on the current code). While a task
  runs on the other provider and `start_provider` resets at a time read from its limit message
  (`reset_known`, saved in `run.json`), the attempt is stopped at the next check
  (`return_mid_task`, `return_check_seconds`), its work committed as WIP, and the task continues on
  `start_provider` with the handoff notes; a cooldown guess never stops an attempt, and with
  `lanes > 1` the dispatcher decides instead. `verifiers_last = true` makes verifiers gate nothing and
  run after every other task (a task that depended on a verifier waits for what that verifier waits for).
- **Examples and docs.** `common.md` and `claude-settings.example.json` now agree: the file no longer
  asks for `sed -n` (use `grep -n` and the Read tool's `offset`/`limit`) or for `git restore`
  style reverts, and the example allows `ruff check`, `mypy`, `npm run lint`/`typecheck` and the
  pnpm/yarn equivalents. `.env.example` is readable again: the deny rules name `.env`, `.env.local`,
  `.env.*.local`, `.env.production`, `.env.development`, `.env.staging`, `.env.test` instead of
  `.env.*`; the placeholder read-only-folder path is gone. README: realistic test times, baton
  never fetches (update `default_base` yourself), disable sleep for overnight runs, why lanes with
  `max_concurrent = 1` may not overlap, `results/` is writable by every agent, and a Housekeeping
  section on pruning `attempt-*.out` and `run.log`.

- **Validation (second round).**
  - Booleans are strict: `commit_leftovers = "false"` (or `enabled`, `effort_flag`, `hardcoded_key_check`,
    `return_to_start_provider`, `preflight.auto`) was read as true; it is now an error that says to drop the
    quotes. Wrong types for `default_base`, `start_provider`, `bin`, `permission_mode`, a rung that is not a
    table, and `depends_on`, `branch`, `base`, `work_type`, `rung`, `feature` in task files are config/task
    errors naming the key, not tracebacks. `baton classify` with a missing sample file is a clean error.
  - `baton plan` checks branch names against the ref-name rules (a task id such as `v1..x` or `x.lock` makes
    an invalid `baton/<id>`; the message says to rename the file) instead of failing at night.
  - `baton plan` warns when `permission_mode` or `extra_args` switch the agent's guardrails off
    (`bypassPermissions`, `--dangerously-skip-permissions`, `danger-full-access`, ...), and when `lanes > 1`
    but no two tasks can run at once (one provider with `max_concurrent = 1`), with the two ways out.
  - `baton run` checks for a running baton before preflight, so a refused second run creates nothing.
    `baton status`/`watch` probe the lock read-only with a shared lock and never create `state/`;
    `baton stop` with no run no longer creates it either; a starting run waits out a probe for up to a second.

- **Commit safety.**
  - WIP is only committed on the task's own branch. A task whose repository cannot be prepared (it holds
    someone's uncommitted changes) no longer commits them onto whatever branch is checked out, and it reports
    0 commits instead of the whole history. `recover()` follows the same rule.
  - Only certain secrets are kept out of WIP commits: `.env*` (not `.env.example`), `*.pem`, `*.p12`, `*.pfx`,
    `*.keystore`, SSH keys, `.pypirc`, and any file that starts like a private key. `secrets.py`,
    `credentials.json`, `*.key` and `.npmrc` are ordinary files and are committed. A refused untracked file is
    added to the repository's `.git/info/exclude`, so it no longer leaves the repository dirty and fails every
    later task there; the task's note names the files that were not committed.
  - `repo-suite` leaves the repository as it found it: files the suite created (`coverage.xml`, `junit.xml`) are
    removed and listed in the report, tracked files it changed are reported, and the branch is put back.
  - A recorded "branch to return to" is dropped when the repository is on a branch baton did not choose.
  - `changed_files` handles renames in `status --porcelain -z`.

- **A model limit closes only that model.** "You've hit your Opus limit" (or a `rate_limit_event` naming a
  model family) is kept per provider and family in `run.json` (`model_limits`); tasks whose rung uses
  another model of that provider keep running there, the Opus ones move to the other provider or wait for
  the family's reset. `baton plan` and SUMMARY.md show it. A limit that names a model the task is not
  using still closes the whole provider, so a task cannot loop on it.
- **Emergency brake per task first.** A task whose own attempts end fast for no known reason
  `max_quick_unknown` times in a row is `failed` and the independent tasks go on (before, the whole run
  stopped after about 20 minutes of waiting). The run still stops, at once, when the program cannot be
  started (exit 126/127), or when `max_quick_unknown` different tasks in a row fail this way
  (`run.json` gains `quick_tasks`; a task state gains `quick_fails`).
- **A clean exit without a result** (exit 0, nothing recognisable, no result file) goes to the no-result
  retries instead of waiting as an "unknown reason".
- **Lanes: a bug in one task** fails that task only (traceback in `run.log`), as in serial mode; the other
  lanes go on. Before, it halted every lane.

- **Strict config.** Unknown keys in the top level, `[run]` and `[providers.*]` are errors with a "did you
  mean" suggestion (they were ignored silently, so `max_sleep_hour = 1` ran with the default). A wrong
  type or range in a number (`task_timeout_min = "abc"`, `max_quick_unknown = 0`, a task's
  `priority = "high"`) is a `ConfigError`/`TaskError` naming the file and the key instead of a traceback.
- **Timezone.** The example config no longer sets `timezone = "UTC"`: Codex prints reset times without a
  zone and they are read in `run.timezone`, which defaults to the machine's zone. `baton plan`/`run`
  warn when a configured zone has a different UTC offset than the machine's. The example's
  `task_timeout_min` now matches the code default (240).
- **Safer example settings.** `claude-settings.example.json` no longer allows `python3`, `node`, `npx`,
  `find`, `cat` or `git restore` (prefix rules let those reach around every deny rule) and denies
  secrets (`.env*`, keys, `~/.ssh`, `~/.aws`), CI files, shells and network tools. Common test runners
  (`pytest`, `npm test`, `go test`, `cargo test`, `make test`) are allowed. README "Safety" now says
  plainly that baton's no-push promise covers baton, not the agent, and recommends a separate user or
  container.
- **Results folder for Claude.** baton adds `results/` to Claude's `--add-dir` list (unless something
  already covers it), so the agent can write `results/<id>.md` outside the repository.
- `baton plan` creates no `results/`, `state/` or `logs/` folder (it checks they could be written), prints the
  first command shell-quoted, and `baton classify` reports an unknown `--timezone` or a bad `--now`
  as an error message. README gains a configuration reference of the `[run]` keys and their defaults,
  and a "First run" section.
- **Parallel lanes (experimental, off by default).** `[run] lanes = N` runs up to N tasks at once, one
  per repository (or per `lane` value in a task's front matter), so Claude and Codex can work at the
  same time. `[providers.*] max_concurrent` caps attempts per provider (default 1). One dispatcher owns
  the queue and the provider status; a limit seen in one lane stops new attempts on that provider in
  all lanes and the interrupted task continues on another one. A busy slot waits; it never moves a task
  to the other provider. Shared emergency brake, SIGTERM to every lane on Ctrl+C with per-lane WIP
  commits, `baton stop` lets busy lanes finish. `lanes = 1` is unchanged (the old loop still runs).
  Log lines get a `[lane]` prefix and `metrics.jsonl` a `lane` field when `lanes > 1`.
  `run.json` is version 2 and gains `running` (version 1 files still load); `baton status` lists
  running lanes, `baton watch --lane NAME` follows one, `baton plan` shows lanes and warns when nothing
  can overlap. See README "Parallel lanes" and `docs/design/parallel-lanes.md`.
- **Signals and processes.**
  - SIGHUP (a closed terminal, an ssh connection that dropped) stops the run like Ctrl+C: the agent is
    ended, the work committed as WIP, the state saved. A terminal that is gone no longer crashes the log.
  - An agent that ignores SIGTERM gets SIGKILL after 30 s; a second Ctrl+C kills at once. Before, baton
    waited for the task's timeout (hours).
  - When an agent exits, whatever it left running in its process group (a dev server, a `nohup` job) is
    ended, SIGTERM then SIGKILL.
  - `run.json` records the pid of each running agent (`agents`). A run that died without cleaning up
    (kill -9, power loss) no longer leaves an agent working in the repository while the next run starts:
    the next `baton run` ends it first (a recycled pid is told apart by its start time).
  - `repo-suite` runs the suite in its own process group: a timeout ends the test runner below the shell
    too, and Ctrl+C no longer waits for the suite.
  - An unexpected exception in the serial loop is written to `run.log` with its traceback and the task
    ends `failed`; the others go on. A wait looks at Ctrl+C every second (it was up to 30).
  - Lanes: the retries a task has used survive a trip back to the dispatcher; an assigned provider no
    longer calls the scheduler's `pick`; `state.running`, `gated_by` and the busy-repo set change under
    the lock.

- **Branches (behaviour change).** A task with no `branch` now works on `baton/<id>` instead of committing
  on whatever branch the repository happens to be on; `baton plan` shows it as `[automatic]` and where it
  starts (`base`, else the current checkout). Every repository is put back on the branch it was on before
  baton touched it when the run ends (remembered in `run.json` as `initial_branches`; a repository with
  uncommitted changes is left as it is, and the log says so). A task that depends on a task in the same
  repository starts from that task's branch when the two branches differ and the file sets no `base`;
  with several different dependency branches `baton plan` warns and the default base stays.
- **WIP commits and preflight.** WIP commits no longer take files that look like secrets (`.env*`, keys,
  `credentials*`; `.env.example` is fine) and the log warns about them and about files over 20 MB. When a
  WIP commit fails, the log says `ERROR could not commit leftovers` and the next task in that repository
  fails with the reason, not just "uncommitted changes". Preflight now refuses a repository without
  `user.name` / `user.email`, and a results/state/logs folder inside a task repository that git does not
  ignore.
- **Re-running a task** (`baton run --only ID`) starts its counters over: `attempts` and `last_end` reset,
  the prompt no longer says the task was started before, and the old `attempt-NN` files move to
  `results/<id>/earlier-N/` instead of being overwritten.
- **Reset times across clock changes.** A reset time in a named zone is resolved with daylight saving in
  mind: a time in the spring gap moves to just after it, a time in the repeated autumn hour takes the later
  one. Without `[run] timezone`, baton now uses the machine's real zone (`$TZ`, `/etc/timezone`,
  `/etc/localtime`) instead of today's fixed UTC offset. The old `Claude AI usage limit reached|<epoch>`
  format now yields its reset time.
- A stop that arrives just before an agent process is started now stops that process too.
- `tests/fake_agent.py` takes the counter under a lock (parallel calls), and new steps `span`, `wait_for`,
  `touch_start`.

- A verifier can write its own `results/<id>.md` again. Its deny rules used to cover the
  whole `results/` folder, and Claude Code applies a Read deny to writes too, so every
  verifier ended "without a result file". Now only the other entries of `results/` (other
  tasks' results and attempt folders, SUMMARY.md, metrics) and `tasks/` are denied.

- `start_provider` defaults to `codex`: a run begins on Codex and moves to Claude when
  Codex is limited. A config without a `[providers.codex]` section begins on the first
  provider it defines. Set `start_provider = "claude"` to keep the old behavior.

## 0.3.0 (2026-10-28)

Summary: the lessons of the real runs of 27-28 October. Codex is probed and left out
of a run when its sandbox cannot start; the run begins from `start_provider` and
tasks return to it after its reset; a handoff carries the previous agent's notes;
the full suite of a repository runs once per feature, a money gate once at the end;
the summary says what was not verified; Codex commits run the hooks and Codex sees
only its own results folder; and `baton stop`, `baton watch` and `run --follow`
make a long run easier to live with. Reinstall (`pip install -e .`) to refresh the
installed metadata: `baton --version` now reads it.

- A task whose `providers` (or `verifier_providers`) exclude the current provider
  runs on an allowed one without moving the run: `current` stays put, the log says
  `F03 runs on claude (providers list; current stays codex)` and never "limited",
  and the next normal task runs on the current provider again.
- `baton --version` reads the installed package metadata (`importlib.metadata`),
  falling back to `pyproject.toml` in a source checkout; the package version is 0.3.0.
- `baton watch`: a live view of the current attempt. It follows the newest
  `results/<task>/attempt-NN.<provider>.out` (the running task's first), prints one
  readable line per Claude or Codex event (what the agent says, the commands and
  tools it runs, failed commands), moves on to the next attempt when the run starts
  one and ends when the run does. `-n` sets the history, `--raw` shows the events,
  `--no-follow` prints the tail and exits.
- `baton run --follow`: when the queue is empty (or only held by an approval
  checkpoint) the run waits (`run.follow_poll_seconds`, default 15) instead of
  ending, picks up task files added to the folders and the decisions of `baton
  approve` / `reject`, and runs what becomes ready. `baton stop` or Ctrl+C ends it.
  Tasks that already ended in this run are not retried; not combinable with `--only`.
- `baton stop`: asks the run in the folder to finish its current task and end
  cleanly (exit code 0, the rest left for the next run). It never interrupts an
  attempt; a wait for a limit reset is cut short with the task left resumable.
  It reports when no run is active.
- Verifier option `when = "feature-end"` (with `feature`): the gate runs once,
  after every implementer task of its feature, instead of gating each dependent;
  the feature's own tasks drop their dependency on it, other features' tasks stay
  gated. `money_gate = true` marks the money gate, and `baton plan` warns when a
  feature has more than one.
- New built-in verifier template `repo-suite`: a repository's full test suite
  (`[repos."<path>"] suite_cmd`, `suite_baseline`, `suite_timeout_min`,
  `suite_failure_regex`) runs once, after the last task of a feature that touched
  that repository, and reports only failures that are not in the baseline file.
  `baton plan` adds it automatically like env-preflight (an approval checkpoint of
  the feature waits for it; work after the checkpoint does not), so the hand-written
  `S-<repo>` tasks go away. Baseline failures are counted under "Not verified".
- A dependency cycle fails the plan with the cycle spelled out (`dependency
  cycle: a -> c -> b -> a`), naming the links baton added itself (the auto-added
  env-preflight, an approval checkpoint) and what to change. Deadlock between a
  checkpoint carrying `feature` and the auto env-preflight was already prevented
  (work that follows a checkpoint is not waited for); a test now pins that.
- `[browser_smoke] deferred = "reason"`: when browser-smoke is intentionally off,
  `baton plan` prints one summary line instead of a warning per UI task, and the
  deferral (with the tasks it affects) is listed under "Not verified".
- "Not verified" section in `results/SUMMARY.md` and `not_verified` in each
  `results/<id>/status.json`: what an agent says it verified only with mocks or
  fixtures or skipped (its result file's `## Not verified` section, which every
  prompt now asks for), the `## Not checked` lines of built-in checks, and UI
  tasks that no browser-smoke follows.
- `real-data-smoke`: `db_env` may be a table of variable = template for a backend
  that reads its database in pieces (`{ MONGO_HOST = "{host}", MONGO_PORT =
  "{port}", MONGO_DB = "{db}" }`; placeholders `{url} {host} {port} {db} {user}
  {password}` come from `smoke_db_url`, literals are allowed, and a table of
  literals needs no `smoke_db_url`). Secret-looking variables and URL credentials
  are never printed.
- Per-repository `forbid_literals`: `[repos."<path>"] forbid_literals = ["regex", ...]`.
  Lines a task adds (any file but tests, `baton: ok` exempts) that match go
  through the same once-only round trip as the built-in hardcoded-key check, and
  apply even when that check is off. An invalid regex fails the config.
- Handoff context: every prompt asks the agent to keep
  `results/<id>.progress.md` up to date (plan, done, next, open questions). When
  a task continues in a new attempt (after a limit switch, a retry or a restart)
  the notes are put in its prompt next to the commits, so the next provider has
  the reasoning and not only the diff. Codex writes `codex-progress.md` in its own
  task folder and baton copies it. A fresh start of a task drops the old notes.
- `start_provider` is no longer overridden by the saved state: a new run with
  no task mid-attempt begins from `start_provider`, and `baton plan` warns when
  the saved `current` differs. `baton run --provider codex` (and `baton plan
  --provider`) overrides it, mid-attempt tasks included.
- Return to the preferred provider: after a switch (say codex to claude), the
  next task that has not started an attempt goes back to `start_provider` once it
  is available again. A task mid-attempt stays where it is.
  `run.return_to_start_provider = false` restores the old sticky behavior.
- Codex sandbox check. `baton plan` and the start of a run probe the Codex
  sandbox (`codex sandbox -- true`; `providers.codex.sandbox_probe` changes the
  arguments, `[]` turns it off; skipped when `sandbox` is empty or
  `danger-full-access`). If it fails with a bwrap error (for example
  `Failed RTM_NEWADDR` after a reboot reset
  `kernel.apparmor_restrict_unprivileged_userns`, or when baton was started from
  inside another agent's sandbox) Codex is left out of the run with the reason and
  the fix (`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, lost on
  reboot). A probe that fails for another reason only warns. At run time, an
  attempt whose output carries the bwrap error and does not end `done` disables
  Codex and retries the task on the next provider instead of ending it blocked.
- `baton plan` checks the Codex model ids of the rungs in use against the
  installed Codex (the `models_cache.json` in its home folder, or
  `providers.codex.known_models`) and fails on an unknown id, suggesting the
  nearest ones. Without a list to check against it warns once.
- Codex no longer gets the whole `results/` folder: it mounts only its task's
  `results/<id>/` and writes `codex-result.md` there, which baton copies to
  `results/<id>.md` (so it cannot read or overwrite other tasks' or verifiers'
  results). `baton plan` refuses a verifier whose providers include `codex`
  (also through `run.verifier_providers`), since Codex cannot be fenced off.
- A Codex task with no result file used to be `done` on the strength of the last
  agent message. That is weak evidence, so `done` is now downgraded to `partial` with a
  note (`blocked` and `partial` are kept as read).
- Codex commits run the repository's hooks (no `--no-verify`) when there is a
  `COMMIT:` line or the task is done. If a hook refuses the commit, baton keeps the
  work as a WIP commit and marks the task `partial` with the hook output.
  Unfinished work is still committed as an unverified WIP commit without hooks.
- WIP commits are named `chore(<id>): wip, <why> (unverified)`, cut to 100 characters,
  so commitlint accepts them (`wip` is not an allowed type).
- Task intent: optional `## User journey` and `## Reuse` sections in a task
  file. `baton plan` warns about a task that touches UI paths (`plan.ui_paths`,
  default `src/app/**` and `*.tsx`; the task's `paths` list and the files its
  text names count) and has neither, and about a UI task that no browser-smoke
  verifier follows. The example `common.md` tells the agent to find and reuse
  the closest existing flow, or say why not.
- New task kind `kind = "approve"`: a human checkpoint. baton writes the open
  questions, each earlier task's journey and result, and the screenshots to
  `results/<id>.md`, holds every dependent while other tasks keep running, and
  continues after `baton approve <id>`; `baton reject <id> --note ...` marks the
  dependents blocked with the note. Decisions are files under
  `state/decisions/`, so they also reach a run in progress.
- Built-in verifier templates (`template = ...`, run by baton without an agent,
  gate like any verifier, blocked when they cannot run): `env-preflight`
  (env keys by name, CORS and redirect allow-lists, loopback host consistency;
  scheduled automatically after the last task of a `feature`), `browser-smoke`
  (Playwright through Node: spinner, console, API host, redirects, screenshots
  with the Reuse page, error messages) and `real-data-smoke` (the main path
  over HTTP against a copy of the dev database).
- Hardcoded data keys: baton scans what an implementer added for literals such
  as `type_key == "person"` and sends the task back once to resolve or justify
  them (`run.hardcoded_key_check`). The error-message rule (no generic error
  without a code, 422 names the field and format, unknown errors show the
  status) is in the example `common.md` and enforced by browser-smoke.
- Skip-ahead treats checkpoints and built-in checks as needing no provider.
- Test fix: the rogue-branch test wrapper no longer checks out a branch in the
  caller's repository during the preflight `--help` probe.
- Design proposal for parallel lanes (`docs/design/parallel-lanes.md`). No
  code yet.
- Codex attempts leave commits to baton because the workspace-write sandbox
  mounts `.git` read-only. Baton uses the result file's `COMMIT:` line as the
  commit message and falls back to its unverified WIP message.
- Every Codex attempt mounts the workspace `results/` directory and configured
  `add_dirs` as writable directories. If no result file is produced, baton can
  recover `STATUS: done`, `partial`, or `blocked` from Codex's last JSON agent
  message.
- Codex review fixes: an implementer's Codex `--add-dir` list drops paths
  hidden from it (a verifier's results), as for Claude; the last agent message
  is read whenever the result file is missing, stale or has no `STATUS`, but
  only after a clean exit 0; a done task with no `COMMIT:` line is committed
  as `chore(<id>): codex changes` instead of an "unfinished" WIP message. Only
  the `COMMIT:` line itself becomes the message; an empty diff makes no commit.
- Example workspace and README no longer carry project-specific content: the
  example routing table uses generic work types, the example timezone is UTC,
  and a test fails if a file in the repository spells out a home folder path.
- SUMMARY.md, which `baton status` prints, is rewritten when a task starts,
  on each attempt and provider switch, before a sleep or retry wait, and on
  resume, not only when a task ends. A running task shows its attempts,
  providers and what it is doing now (attempt N on codex, sleeping until
  22:48, retrying in 30s) instead of looking pending.
- Log wording: a task restricted by `providers` or `verifier_providers` that
  waits now logs "allowed providers (claude) limited; codex not allowed for
  this task" instead of "all providers limited".
- Skip-ahead scheduling: when the next task is waiting for its allowed
  providers to reset, baton runs a later ready task instead of sleeping.

## 0.2.0

- Per-task `base`: `baton plan` shows the base each new branch starts from. A
  task may branch from a branch that a task it depends on creates (stacked
  branches).
- Verifiers: hidden verification tasks in `verifiers/` (`kind = "verify"`),
  run in a fresh session on their own branch, fenced off from implementer
  output and hidden from implementers on Claude. A verifier may change test
  files only (`run.verifier_test_paths`), or it ends blocked.
- Gate: tasks that depend on a verifier run only after it is `done`; the
  others are reported as gated.
- `run.verifier_providers` (default `["claude"]`): a verifier never falls back
  to another provider; it waits for a reset or ends blocked.
- `baton run --only` refuses a task gated by an unfinished verifier unless
  `--force` is given.
- Per-task `providers` list: only those providers run the task. It never
  falls back outside the list; it waits for a reset or ends blocked.
  Verifiers default to `run.verifier_providers`.

## 0.1.0

- Initial import: task queue, Claude/Codex switching on usage limits, limit
  detection, WIP commits for unfinished work, emergency brake, metrics.
