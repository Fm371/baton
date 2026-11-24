# Configuration reference

Every key baton reads, with its type, default and meaning. `baton.toml` is checked strictly: an unknown key, a
value of the wrong type or an out-of-range number stops baton with a message naming the file and the key (a
misspelled key suggests the nearest valid one) instead of silently running with a default. Paths are relative to
the folder of `baton.toml` unless they are absolute; `{base_dir}` expands to that folder in `common.md`, task
bodies, a task's `repo` and the Claude settings file.

Sections: [`[run]`](#run) · [`[providers.*]`](#providers) · [`[routing]`](#routing) ·
[`[services.*]`](#services) · [`[preflight]`](#preflight) · [`[repos."path"]`](#repospath) · [`[plan]`](#plan) ·
[`[browser_smoke]` and `[real_data_smoke]`](#browser_smoke-and-real_data_smoke) · [task front matter](#task-front-matter)

Most changes need a restart of the run (`baton stop`, edit, `baton run`): baton reads `baton.toml` when it
starts. The exceptions are read again before each new attempt of a task: the routing table, timeouts and retry
limits, `verifier_test_paths`, and each provider's `extra_args`, `env`, `settings`, `permission_mode`,
`effort_flag`, `add_dirs`, `sandbox`, `profile` (what changed is logged). Changes to account names, `type`,
`home`, `bin`, `enabled`, provider order, the folders and `lanes` take effect when the run starts again.

## [run]

| key | type | default | meaning |
|---|---|---|---|
| `start_provider` | string | first enabled name in `provider_order`, otherwise `"codex"` when enabled, otherwise the first enabled provider | account a run begins on |
| `provider_order` | list of account names | `[]` | priority order after `start_provider`. Enabled accounts omitted from the list are appended, with the built-in names before named accounts |
| `default_base` | string | none | branch new task branches are created from (a task's `base` overrides it); baton never fetches, so it must exist locally |
| `timezone` | string (IANA name) | this machine's zone | zone for reset times printed without one (Codex). Leave it out unless you know the CLI prints another zone: a zone that is not your machine's misreads resets by hours, and `baton plan` warns |
| `task_timeout_min` | number > 0 | `240` | elapsed-time cap per attempt, minutes; monotonic, excluding suspend/long observation gaps (see operations) |
| `max_turns` | whole number ≥ 0 | `0` (off) | Claude `--max-turns` per attempt (a task can set its own) |
| `reset_margin_seconds` | whole number ≥ 0 | `180` | wait this long past a printed reset time |
| `unknown_limit_cooldown_min` | number ≥ 0 | `30` | cooldown for a limit with no readable reset time |
| `max_sleep_hours` | number > 0 | `170` | stop instead of sleeping longer than this (a weekly limit fits) |
| `quick_exit_seconds` | number ≥ 0 | `120` | an exit faster than this with no known reason counts as a quick failure |
| `max_quick_unknown` | whole number ≥ 1 | `3` | this many quick failures of **one task** fail that task; this many **different** tasks in a row stop the run |
| `transient_wait_seconds` | number ≥ 0 | `600` | wait after an overloaded / 429 error before retrying the same provider |
| `max_transient_retries` | whole number ≥ 0 | `3` | transient retries per task |
| `no_result_retries` | whole number ≥ 0 | `1` | retries of an attempt that ended without a result file |
| `commit_leftovers` | bool | `true` | commit unfinished work as `chore(<task>): wip, ... (unverified)`; never stash |
| `common_prompt` | path | none | file prepended to every task prompt (`common.md`) |
| `tasks_dir` | path | `tasks` | folder of task files |
| `verifiers_dir` | path | `verifiers` | folder of verifier files (hidden from implementers) |
| `results_dir` | path | `results` | result files, `SUMMARY.md`, `metrics.jsonl`, attempt output |
| `state_dir` | path | `state` | `run.json`, `run.json.bak`, lock, decisions, fixtures |
| `logs_dir` | path | `logs` | `run.log` |
| `watch_repos` | list of paths | `[]` | repositories to warn about when a task leaves changes in them |
| `verifier_test_paths` | list of globs | `tests/**`, `**/__tests__/**`, `**/*.test.*`, `**/test_*.py` | the only files a verifier may change; also excluded from the hardcoded-key scan |
| `verifier_providers` | list of providers | `["claude"]` | providers a verifier may run on (never falls back outside the list) |
| `restore_branches` | bool | `true` | at the end of a run put each repository back on the branch it was on before baton touched it; `false` leaves it on the last task branch (useful when you test a task's branch while the run is going) |
| `allow_codex_verifiers` | bool | `false` | accept `codex` in `verifier_providers` / a verifier's `providers`. Codex cannot hide files from the agent, so the verifier may see implementer output; `baton plan` warns instead of refusing, and the test-path check still runs |
| `hardcoded_key_check` | bool | `true` | flag new string literals that look like data keys, once per task |
| `return_to_start_provider` | bool | `true` | for a new task, return to an available higher-priority account the task may use |
| `return_mid_task` | bool | `false` | **experimental**: stop an attempt on the other provider once `start_provider` resets (known reset time) and hand the task back; not with `lanes > 1`; a task can set its own |
| `return_check_seconds` | number > 0 | `60` | how often a running attempt checks for that reset |
| `verifiers_last` | bool | `false` | verifiers that **no task depends on** run after every other task; a verifier somebody lists in `depends_on` stays a real gate |
| `existing_branch` | `"fail"`, `"warn"`, `"rebase"` | `"fail"` | a task branch that already exists but lacks the work of a task it depends on (or shares no history with its base): refuse it, use it anyway with a log line, or rebase it onto the dependency first (a conflict aborts the rebase and refuses the task) |
| `follow_poll_seconds` | number > 0 | `15` | how often `baton run --follow` looks for new tasks and approvals |
| `lanes` | whole number ≥ 1 | `1` | tasks that run at once, in different repositories ([lanes.md](lanes.md)) |
| `required_env` | list of names | `[]` | environment variables every agent task needs set (shell or `[providers.*] env`); `plan` fails and no attempt is spent without them. Also allowed in a provider table and a task file |
| `min_version` | string | unset | `baton plan` warns when the installed baton is older than this (a run records the version it used in `run.json` and on the first line of `SUMMARY.md`) |
| `min_free_mb` | number ≥ 0 | `500` | `plan` and `run` warn when the folder holding `results/` has less free space than this (0 = off) |
| `stall_minutes` | number ≥ 0 | `0` (off) | end an attempt whose stdout and stderr have not grown for this long, keep WIP and retry once; a second stall fails (budget survives restart). A task can override or use `0` to opt out; possible suspend gaps grant a fresh interval, see operations |
| `max_limit_cycles` | whole number ≥ 1 | `6` | a task fails after this many consecutive usage-limit outcomes with no repository changes, once there is no available account it has yet to try |
| `wip_secret_patterns` | list of globs | `.env`, `.env.*` (not `.env.example`/`.sample`/`.template`/`.dist`), `*.pem`, `*.p12`, `*.pfx`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `.pypirc`, plus any file that starts like a private key | file names a WIP commit **never** takes (untracked ones go to `.git/info/exclude`); `[]` switches the tier off |
| `wip_warn_patterns` | list of globs | `*.key`, `credentials*`, `secrets.*`, `.npmrc`, `*.token`, `*.secret`, `service-account*.json`, `.netrc`, `.htpasswd` | file names a WIP commit takes but **reports loudly** (log, task note, `## Warnings` in `SUMMARY.md`); `[]` switches the tier off. A glob with a `/` is matched against the whole path from the repository's top folder; case is ignored |
| `env_limited_action` | `"note"`, `"retry_other_provider"` | `"note"` | what to do with a `partial`/`blocked` result caused by a sandbox limit: only label it, or give the task one more attempt on the other provider (never with `lanes > 1`) |
| `services_for_all_tasks` | bool | `false` | check every `[services.*]` before every task, not only those a task `needs` |
| `on_event` | command string or list of arguments | unset | command run (never through a shell) on task end, long sleep, run end/stop/death, with `BATON_EVENT`, `BATON_TASK`, `BATON_STATUS`, `BATON_NOTE`, `BATON_RUN_ID` in the environment ([environment.md](environment.md)) |
| `on_event_sleep_min` | number ≥ 0 | `30` | a wait for a reset at least this long is a `sleep` event |
| `on_event_timeout_seconds` | number > 0 | `15` | time limit for one `on_event` command |

## [providers.*]

Keep using `[providers.claude]` and `[providers.codex]` for their usual CLI logins. Define additional
accounts under `[providers.<name>]` with `type = "claude"` or `type = "codex"` and a separate CLI home per
account. Account names use ASCII letters, digits, hyphens and underscores and must begin with a letter or digit.
Each name has its own usage-limit state and concurrency slots.

| key | for | type | default | meaning |
|---|---|---|---|---|
| `type` | both | `"claude"` or `"codex"` | inferred for `[providers.claude]` and `[providers.codex]` | CLI used by a named account |
| `bin` | both | string | the CLI name | the executable; a relative path with a slash is relative to `baton.toml`. Point it at a wrapper to run the tool in a container |
| `enabled` | both | bool | `true` | `false` leaves the provider out of the run |
| `extra_args` | both | list of strings | `[]` | extra command-line arguments |
| `env` | both | table of strings | `{}` | environment variables set for the agent (also count for `required_env`) |
| `required_env` | both | list of names | `[]` | variables that must be set before this provider runs a task |
| `add_dirs` | both | list of paths | `[]` | extra folders the agent may read (passed as `--add-dir`). baton adds `results/` itself: the agent writes `results/<id>.md` there |
| `max_concurrent` | both | whole number ≥ 1 | `1` | attempts at once on this provider (only matters with `lanes > 1`) |
| `settings` | Claude | path | none | the allow/deny rules file (`claude-settings.json`); required in practice, see [safety.md](safety.md) |
| `permission_mode` | Claude | string | `"acceptEdits"` | Claude's `--permission-mode`; `baton plan` warns about modes that switch the guardrails off |
| `effort_flag` | Claude | bool | `true` | set `false` if your Claude Code has no `--effort` flag |
| `sandbox` | Codex | string | `"workspace-write"` | Codex's `--sandbox`; `""` when a wrapper provides its own |
| `profile` | Codex | string | none | a `[profiles.<name>]` block in `~/.codex/config.toml` |
| `home` | Codex/Claude | path | the tool's default for built-in providers | for a named account, its CLI home, also accepted as `env.CODEX_HOME` or `env.CLAUDE_CONFIG_DIR`. For the built-in providers, `home` controls where Baton looks for session files |
| `known_models` | Codex | list of strings | `[]` (read Codex's own `models_cache.json`) | model ids `baton plan` accepts |
| `sandbox_probe` | Codex | list of strings | the built-in probe | arguments (after the binary) that probe whether the sandbox starts; `[]` turns the probe off |
| `network` | Codex | bool | `false` | let commands inside the `workspace-write` sandbox use the network (adds `-c sandbox_workspace_write.network_access=true`); it opens the whole internet, not only localhost |

### Codex Business, then Personal

Merge these settings into your existing `baton.toml`. Keep your existing Claude provider section and
append any other enabled accounts to `provider_order` to set their priority explicitly.
If `[providers.codex]` already signs in to one of these workspaces, set `enabled = false` in that
section so Baton tries each workspace once.

```toml
[run]
start_provider = "codex-business"
provider_order = ["codex-business", "codex-personal"]

[providers.codex-business]
type = "codex"
home = "~/.codex-business"

[providers.codex-personal]
type = "codex"
home = "~/.codex-personal"
```

Create each directory and sign in from a terminal. Choose Business in the first browser sign-in and
Personal in the second. A named Claude account uses the corresponding `CLAUDE_CONFIG_DIR` for its CLI.

```bash
mkdir -p -m 700 "$HOME/.codex-business" "$HOME/.codex-personal"
CODEX_HOME="$HOME/.codex-business" codex login
CODEX_HOME="$HOME/.codex-personal" codex login
```

Run `baton plan` to check the routing, then `baton run`. When the Business CLI reports a usage limit,
Baton records that account's reset time and continues the task on the next available account. Once
Business is available again, new tasks prefer it. `baton run --provider codex-personal` puts Personal
first for one run. `baton providers reset codex-personal` clears the limit recorded for that account.

In task front matter and `run.verifier_providers`, `"codex"` and `"claude"` select all accounts of that
CLI type. Use a named account such as `providers = ["codex-business"]` to pin a task to it.

## [routing]

A **rung** is one step of the ladder, with the equivalent model on each provider; a task that moves to the other
provider keeps its rung (never a lower one). A **work type** maps a name to a rung, so task files say what the
work is and the table decides the model.

```toml
[routing.rungs.r3]
claude = { model = "sonnet", effort = "medium" }
codex  = { model = "gpt-6.1-sol", effort = "medium" }

[routing.work_types]
crud = "r3"
```

| key | meaning |
|---|---|
| `[routing.rungs.<rung>.<provider>]` | `model` (required) and `effort` (optional string). A named account uses the model for its CLI type unless the rung defines a model under its account name |
| `[routing.work_types]` | `<work type> = "<rung>"`; the rung must exist |

At least one rung is required. A task names `work_type = "..."` or `rung = "..."` in its front matter.

## [services.*]

Something the tasks need running (a database, a container). See [environment.md](environment.md).

| key | type | default | meaning |
|---|---|---|---|
| `check` | command (string or list) | required | exit code 0 = healthy; a string runs through `/bin/sh` |
| `restart` | command | none | tried once when the check fails |
| `timeout_s` | number > 0 | `30` | per check |
| `endpoint` | string | none | told to the agents ("mongo at 127.0.0.1:27017") so they do not look for another instance |
| `wait_s` | number ≥ 0 | `5` | pause after a restart, before the second check |

The name may use letters, digits, `.`, `_` and `-`. Commands come from `baton.toml` only, never from a task file.

## [preflight]

What the env-preflight check reads ([verifiers.md](verifiers.md#env-preflight)), plus the sandbox reachability
list. Nothing here is a secret; env files are read for key names only.

| key | type | default | meaning |
|---|---|---|---|
| `repos` | list of paths | `[]` | repositories checked against their `.env.example` (the check is configured when this is non-empty) |
| `backend` | path | none | the repository whose env files hold the CORS and redirect lists |
| `panel_origins` | list of URLs | `[]` | origins every CORS and redirect list must contain |
| `auth_callback` | URL | none | the auth callback; `localhost` and `127.0.0.1` must not be mixed with the origins |
| `cors_keys` | list of names | the usual names (`CORS_ORIGINS`, ...) | env keys to look under for the CORS list |
| `redirect_keys` | list of names | the usual names (`ALLOWED_REDIRECT_URLS`, ...) | env keys for the redirect allow-list (`[]` skips that check) |
| `env_files` | list of names | `.env`, `.env.local`, `.env.development`, `.env.development.local` | env files to read |
| `auto` | bool | `true` | schedule env-preflight automatically after each `feature` |
| `services_tcp` | list of `host:port` | `[]` | endpoints the agents' sandbox must reach; `baton plan` connects from this machine and from inside the Codex sandbox and fails when the sandbox cannot |

## [repos."path"]

Per-repository rules; the key is the repository path.

| key | type | default | meaning |
|---|---|---|---|
| `forbid_literals` | list of regexes | `[]` | no added line (outside tests) may match; same once-only round trip as the hardcoded-key check |
| `suite_cmd` | command (string or list) | none | the repository's full test suite, run once after the last task of each feature that touched it |
| `suite_baseline` | path | none | known failing tests, one per line (`#` comments); only new failures are reported |
| `suite_timeout_min` | number > 0 | `60` | time limit for the suite |
| `suite_failure_regex` | regex with one group | pytest/unittest/go/jest | the group is the name of a failing test |

## [plan]

| key | type | default | meaning |
|---|---|---|---|
| `ui_paths` | list of globs | `src/app/**`, `*.tsx` | a task whose text or `paths` touches these should carry `## User journey` or `## Reuse`; `baton plan` warns otherwise (a pattern without a slash matches the file name at any depth) |

## [browser_smoke] and [real_data_smoke]

Settings of the built-in verifier templates of the same name; a verifier's own `params` override them. Their keys
(`node_dir`, `storage_state`, `api_origins`, `spinner_selector`, `servers`, `deferred`; `smoke_db_url`,
`db_env`, `prepare_cmd`, `server`, `base_url`, `allow_reuse`) are described, with examples, in
[verifiers.md](verifiers.md#browser-smoke).

## Task front matter

Keys of the `+++ ... +++` block of a task or verifier file ([tasks.md](tasks.md)).

| key | type | default | meaning |
|---|---|---|---|
| `id` | string | the file name without `.md` | ASCII letters, digits, `.`, `_`, `-`; starts with a letter or digit; becomes part of branch and file names |
| `repo` | path | `.` | the repository (relative to `baton.toml`, or absolute; `{base_dir}` works) |
| `work_type` | string | none | a key of `[routing.work_types]` (or use `rung`) |
| `rung` | string | none | a key of `[routing.rungs]`, instead of `work_type` |
| `kind` | `"implement"`, `"verify"`, `"approve"` | `"implement"` | `verify` files live in the verifiers folder; `approve` is a human checkpoint with no agent |
| `branch` | string | `baton/<id>` (`verify/<id>` for a verifier) | the task's branch, created from `base` if missing |
| `base` | string | `[run] default_base` | branch to create the task branch from |
| `depends_on` | list of ids | `[]` | tasks (and verifiers) that must finish first; a task starts from the branch of a same-repository dependency |
| `after` | list of ids | `[]` | run order only: wait until these tasks have ended, whatever their result. Unlike `depends_on` it does not gate, skip, or require their branch to be contained in this task's branch; use it across branches of one repository |
| `settings` | path | – | Claude settings file for this task only (relative to `baton.toml`'s folder), instead of `providers.claude.settings`; baton still adds its deny rules |
| `sandbox` | string | – | Codex sandbox mode for this task only (for example `danger-full-access` for the one task that must write to `.git`), instead of `providers.codex.sandbox` |
| `creates_base` | bool | `false` | this task is meant to create (or commit straight to) the default base or a main line, for example an import task: `baton plan` does not warn that it commits onto a main line |
| `priority` | whole number | `0` | higher first among ready tasks |
| `timeout_min` | number > 0 | `[run] task_timeout_min` | time limit per attempt |
| `max_turns` | whole number ≥ 0 | `[run] max_turns` | Claude only; `0` = use the run's value |
| `stall_minutes` | number ≥ 0 | `[run] stall_minutes` | per-task stall limit (`0` = off for this task) |
| `return_mid_task` | bool | `[run] return_mid_task` | `false` keeps this task on the provider it runs on (a large migration that is costly to stop halfway); `baton plan` shows `return: off` |
| `providers` | list of providers | all enabled | only these providers may run it (no fallback outside the list) |
| `feature` | string | none | tasks sharing a feature get an env-preflight and a repo-suite after the last of them |
| `paths` | list of globs | `[]` | extra paths the task touches (for the UI-path warning) |
| `lane` | string | the repository | with `lanes > 1`, tasks with the same lane never overlap ([lanes.md](lanes.md)) |
| `needs` | list of service names | `[]` | `[services.*]` that must be healthy before the task starts |
| `required_env` | list of names | `[]` | environment variables this task needs set |
| `review` | bool | `false` | opt in to a cross-provider review: `baton review --pending` reviews it once it is done. Never run by `baton run` ([operations.md](operations.md#cross-provider-review)) |
| `template` | `"env-preflight"`, `"browser-smoke"`, `"real-data-smoke"`, `"repo-suite"` | none | a built-in check baton runs itself (verifiers only) |
| `params` | table | `{}` | settings of that template; they override the workspace section of the same name |
| `when` | `"feature-end"` | none | with `feature`, run the verifier once after the feature's last implementer task |
| `money_gate` | bool | `false` | marks the verifier that checks money; `plan` warns when a feature has more than one |
