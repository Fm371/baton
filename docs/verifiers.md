# Verifiers and built-in checks

## Verifiers

A verifier is a task that checks someone else's work without seeing how it
was done, for example an independent check of billing logic with canary
mutations the implementer must never learn about.

- Put it in `verifiers/` with `kind = "verify"` in the header; the format is
  the same as a task. Verifiers share the dependency graph with tasks: a
  verifier usually `depends_on` the tasks it checks, and later tasks may
  depend on the verifier.
- It runs in a fresh session on its own branch (`verify/<id>` unless `branch`
  is set), created from the branch of the task it depends on. With several
  dependencies in its repository on different branches, `baton plan` refuses
  unless the verifier sets `base`.
- Its prompt is `common.md` plus the verifier file. Claude runs get a settings
  file that denies reading `results/` and `tasks/`.
- Implementer runs on Claude get deny rules for `verifiers/` and for each
  verifier's results, the verifiers folder is never passed with `--add-dir`
  (`baton plan` refuses an `add_dirs` entry inside it), and its path never
  appears in a prompt.
- Contract: a verifier may add or change test files only. After it ends,
  baton diffs its branch against where it started; a path outside
  `run.verifier_test_paths` (default `tests/**`, `**/__tests__/**`,
  `**/*.test.*`, `**/test_*.py`) makes it **blocked**.
- Gate: when a verifier does not finish `done`, every task that depends on
  it, directly or through other tasks, is **gated** and does not run.
  `baton run --only <task>` refuses a gated task (exit code 5) unless
  `--force` is given.
- **A gate that runs once, at the end of the feature.** Give a verifier
  `feature = "pay"` and `when = "feature-end"`: it then waits for every
  implementer task of that feature (not those a checkpoint precedes) and runs once,
  instead of holding back the tasks that depend on it. The feature's own tasks drop
  their dependency on it; tasks of other features that depend on it stay gated.
  Mark the verifier that checks money with `money_gate = true`: `baton plan` warns
  when a feature has more than one.
- `verifiers_last = true` moves the verifiers that **nobody depends on** to the end of the
  run, after every other task. A verifier that another task lists in `depends_on` stays a
  real gate in its normal place: that task waits for it, and is gated if it does not end
  `done`. Use it to push the final, expensive checks (a money gate nobody builds on) to the
  end of a tight quota; the price is that a problem such a verifier finds may already be
  built upon.
- `baton plan` marks verifiers `[verify]` and never prints their text.
- Verifiers run only on `run.verifier_providers` (default `["claude"]`, the
  provider that can be fenced off). A verifier never falls back to another
  provider: when its providers are limited it waits for their reset, and when
  none can run (disabled, auth error, or a reset beyond `max_sleep_hours`) it
  ends **blocked** with the reason.

Known gaps:

- **Codex cannot be fenced off.** `codex exec` has no read restriction (its
  sandbox limits writes and network, not reads), so a Codex implementer could
  read `verifiers/` if it went looking. baton keeps the path out of its prompt
  and command line, and mounts only the task's own `results/<id>/` folder as
  writable (Codex writes its result file there and baton copies it to
  `results/<id>.md`), not the whole `results/` tree. That is why verifiers run
  on Claude only: `baton plan` refuses a verifier whose providers include
  `codex`. If the implementers must be fenced off too, disable Codex for that run.
- Claude's `Bash(...)` deny rules match the absolute path in a command; a
  relative path after `cd` is not caught. `Read` rules also cover Grep and
  Glob.
- A verifier's commits stay on its own branch, which later tasks in the same
  repository can see in `git log --all`. **A verifier must never commit canary
  or mutation descriptions or its own verification file**, and its tests are
  named by the behavior they check, not by the canary that motivated them.
  baton tells the verifier so in its prompt; it cannot check it.

## Built-in checks

Some verification needs no agent: it reads files, opens a browser or calls an
API. A verifier with `template = ...` is run by baton itself, on no branch and
no provider. It writes `results/<id>.md` (`STATUS: done`, or `STATUS: blocked`
with the gaps), so everything above applies: a verifier that is not `done`
**gates** every task that depends on it. A check that cannot run (no Playwright,
no `smoke_db_url`, a server that will not start) is blocked with the reason; it
never passes by default.

```markdown
+++
kind = "verify"
template = "browser-smoke"
depends_on = ["30-join-page"]
[[params.routes]]
path = "/join?token=abc"
+++
```

`repo` and a body are optional for these. `params` carries the verifier's
settings and overrides the workspace section of the same name.

### env-preflight

Why: a feature that is correct in code still fails on a missing `.env.local`,
a CORS list without the new panel, or `localhost` against `127.0.0.1`. Read
only; env files are read for key **names**, values are never printed.

```toml
[preflight]
repos = ["../api", "../admin", "../partner-panel", "../user-panel"]
backend = "../api"                          # its env files hold the two lists below
panel_origins = ["http://127.0.0.1:3001", "http://127.0.0.1:3002"]
auth_callback = "http://127.0.0.1:8000/auth/callback"
# cors_keys = ["CORS_ORIGINS", ...]         # env keys to look under (defaults cover the usual names)
# redirect_keys = ["ALLOWED_REDIRECT_URLS", ...]   # empty list: skip the redirect check
# env_files = [".env", ".env.local", ...]
# auto = false                              # do not schedule it automatically
```

- every key of each repo's `.env.example` (a key whose comment says `optional`
  is exempt) is set and non-empty in `.env`, `.env.local`, ...;
- the backend's CORS list contains every panel origin (`*` passes);
- the backend's auth redirect allow-list contains every panel origin;
- `localhost` and `127.0.0.1` are not mixed across the callback URL, the panel
  origins and those two lists.

It reports one consolidated list of gaps. Tasks that share a `feature =
"..."` value get an `env-preflight-<feature>` verifier **scheduled
automatically** after the last of them (`baton plan` shows it as `[verify:
env-preflight, auto]`), and an approval checkpoint in the feature waits for it,
so the gaps are listed before a human starts QA. Write your own
`template = "env-preflight"` verifier with the same `feature` to replace it.

### browser-smoke

Why: unit tests that mock `useAuth` do not notice a route with no auth
provider, an endless spinner, or a page that does not look like its neighbours.
Needs Node and Playwright (`npm i -D playwright` where the app lives).

```toml
[browser_smoke]
node_dir = "../user-panel"            # cwd of the driver; `playwright` resolves from here
storage_state = "qa/test-user.json"         # Playwright storage state of a TEST account
api_origins = ["http://127.0.0.1:8000"]     # fetch/xhr calls must go here
spinner_selector = "[aria-busy=true], .spinner"
servers = [{ name = "panel", cwd = "../user-panel", cmd = "npm run dev", url = "http://127.0.0.1:3002" }]
```

and in the verifier, one `[[params.routes]]` per new or changed route:

```toml
[[params.routes]]
path = "/join?token=abc"
logged_out_redirect = "/login"              # the redirect that is expected, if any
reuse = "/invitations"                      # the "## Reuse" reference page, screenshotted too
expect_endpoints = ["/api/plans"]           # must be called when logged in
[params.routes.invalid_submit]
steps = [{ fill = "#email", value = "x" }, { click = "button[type=submit]" }]
message_selector = "[role=alert]"
expect_field = "email"
```

baton starts the servers (or reuses ones that already answer, and stops only
what it started), opens each route logged out and logged in, and fails on: an
endless spinner (still there after `spinner_timeout_s`), console errors, an API
call that leaves `api_origins` or answers 5xx, a missing `expect_endpoints`
call, an unexpected redirect, and a vague message after the invalid submit (see
below). Screenshots go to `results/<id>/`, with one of the `reuse` page next to
each route's for comparison; an approval checkpoint that depends on the verifier
lists them. Use a dedicated test account for `storage_state`, never your own.
Gate the tasks after a frontend task on it by making them `depends_on` the
verifier; `baton plan` warns about a UI task that no browser-smoke verifier
follows. If browser-smoke is off on purpose (Playwright is not approved yet), say
so once with `[browser_smoke] deferred = "reason"`: `baton plan` then prints one
summary line instead of a warning per UI task, and the deferral and the tasks it
affects are listed under "Not verified". Verifiers that do exist still run.

### real-data-smoke

Why: fixtures create the keys the code hardcodes, so tests pass; the real
database names them differently and the endpoint returns 400. This walks the
feature's main path over HTTP against a **copy** of the dev database.

```toml
[real_data_smoke]
smoke_db_url = "mongodb://localhost:27017/app_smoke"   # or env BATON_SMOKE_DB_URL
db_env = "MONGO_URL"            # the variable the backend reads its database URL from
# A backend that reads host, port and name separately takes a table instead. Each value
# is a literal or uses {url} {host} {port} {db} {user} {password} of smoke_db_url; with
# only literals there is no smoke_db_url to set:
# db_env = { MONGO_HOST = "{host}", MONGO_PORT = "{port}", MONGO_DB = "{db}" }
# db_env = { MONGO_HOST = "127.0.0.1", MONGO_DB = "app_smoke" }
# prepare_cmd = "scripts/refresh-smoke-db.sh"          # makes the copy; gets SMOKE_DB_URL
server = { name = "api", cwd = "../api", cmd = "uvicorn app.main:app --port 8000", url = "http://127.0.0.1:8000/docs" }
base_url = "http://127.0.0.1:8000"
```

```toml
[[params.steps]]
name = "log in"
method = "POST"
path = "/auth/login"
json = { username = "qa", password = "${QA_PASSWORD}" }   # ${VAR} comes from the environment, never printed
capture = { token = "access_token" }
[[params.steps]]
name = "enroll"
method = "POST"
path = "/plans/{plan_id}/enroll"            # {name}: a captured value
headers = { Authorization = "Bearer {token}" }
expect_status = [200, 201]
```

baton starts the backend with `db_env` set to `smoke_db_url` and refuses to
use a server that is already running (it may point at the dev database) unless
`allow_reuse = true`. Credentials in the URL are never printed. The first step
that fails stops the walk and the report quotes its exact status and response.

### repo-suite

Inside a task the agent runs targeted tests only. The repository's **full** suite
runs once, after the last task of the feature that touched that repository, and
only **new** failures are reported. This replaces hand-written `S-<repo>` tasks.

```toml
[repos."../api"]
suite_cmd = "pytest -q -rf"              # a shell command, or a list of arguments
suite_baseline = "baselines/api.txt"     # known failing tests, one per line (`#` comments; may be empty)
suite_timeout_min = 60
# suite_failure_regex = "^not ok \\d+ - (.+)$"   # one group = the failing test; default reads pytest, unittest, go and jest
```

`baton plan` adds `repo-suite-<feature>-<repo>` after the last task of each
`feature` for every repository with a `suite_cmd`, on the branch of that last task;
an approval checkpoint of the feature waits for it, work after the checkpoint does
not. Write a `template = "repo-suite"` verifier with `repo = "../api"` yourself
to place it differently (the feature then gets no auto-added one for that repo).
Run it with a flag that lists failing tests (`-rf` for pytest): a failing suite
whose output names no test blocks with its last lines, since new failures could
not be told from old. The baseline's failures are counted under "Not verified".

## Hardcoded keys and error messages

Two rules the agent gets in `common.md`, with a check behind each:

- **Data keys.** When an implementer task ends `done` or `partial`, baton scans
  the lines it added (test files excluded, `run.verifier_test_paths`) for
  string literals that look like database keys: `type_key == "person"`,
  `{"key": "person"}`, `resource_type="person"`. If it finds any and the result
  file has no `## Hardcoded keys` section explaining them, the task goes back
  to the agent **once** in a new session with the list; whatever remains is
  noted on the task and in `results/<id>/hardcoded-keys.md`. Add `baton: ok` to
  a line to exempt it, or set `run.hardcoded_key_check = false`.
- **Forbidden literals, per repository.** On top of that check, a repository can
  list regexes that no added line may match (a production host, a real tenant
  id, a database name), in any file that is not a test:

  ```toml
  [repos."../api"]
  forbid_literals = ["prod-db\\.internal", "tenant_[0-9a-f]{8}"]
  ```

  A match goes through the same once-only round trip, and `baton: ok` exempts a
  line. It applies even when `hardcoded_key_check` is off.
- **Error messages.** No generic message ("try again") without an error code; a
  422 names the field and the expected format; an unknown error shows the HTTP
  status. browser-smoke applies this to the message shown after the invalid
  submit of each route.

## Not verified

`results/SUMMARY.md` ends with a **Not verified** section, so QA starts where the
checks stopped. It gathers what each task's result file lists under `## Not
verified` (every prompt asks for it: what was checked only with mocks or fixtures,
what was skipped or could not run), the `## Not checked` lines of the built-in
checks, and a UI task that no browser-smoke follows. The same list is in
`results/<id>/status.json` as `not_verified`.
