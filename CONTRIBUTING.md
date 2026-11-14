# Contributing to baton

baton runs unattended with write access to other people's repositories, so changes are judged by one
question: does this make a bad night less likely or easier to understand the next morning? Small,
tested, boring changes are welcome.

## Setup

Python 3.11+, git, and nothing else (baton has no runtime dependencies).

```bash
git clone https://github.com/Fm371/baton && cd baton
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # ruff, build, twine
git config user.name "you"; git config user.email "you@example.com"    # the tests commit in temp repos
```

## The checks (CI runs the same)

```bash
ruff check baton tests tools                     # real mistakes only: unused/undefined names, B-rules
python tools/quality_budget.py                   # the size/complexity ratchet, see below
python -m tests.run_parallel                     # one process per test module, about 100 s on 4 cores
python -m unittest discover -s tests -t .        # the same tests in one process, about 5 min
python -m unittest tests.test_limits             # one module while you work
```

## The quality budget

`tools/quality_budget.py` fails when a function in `baton/` has a cyclomatic complexity above 15 or is
longer than 80 lines, unless it is listed in `tools/quality_baseline.json`, and it fails when a listed
function gets worse. So the code that is already too big may not grow, and new code starts within the limits.

- Over the limit? Split the function; do not add it to the baseline.
- You made a listed function smaller: run `python tools/quality_budget.py --update` and commit the lower
  numbers (the update refuses to raise a value).
- A rise that is truly unavoidable: `--update --force`, with the reason in the commit message.

## Tests: fake CLIs, and what that does not prove

The suite never starts a real `claude` or `codex`. `tests/fake_agent.py` plays both: a scenario file
(`BATON_FAKE_SCENARIO`) says what each attempt does (commit, write a result file, hit a limit, hang, ignore
SIGTERM, ...), and the fixtures build real git repositories in temp folders with a fake clock. That makes the
suite fast and deterministic, but **how the real CLIs behave is unverified**: their output formats, their
sandbox, their limit messages. Keep that in mind when you read "all tests pass".

Rules:

- A behaviour change comes with a test that fails without it. Prefer asserting what a user can observe (the
  branch, the commits, the files, the log line, the exit code) over private fields.
- **Limit and error messages from real runs go into `tests/fixtures/limits/`** as one file per sample, named
  `<provider>-<kind>.txt` (`*.stderr.txt` is fed as stderr), with its expected outcome in `FIXTURES` in
  `tests/test_limits.py`. Never invent a "real" message: mark a synthetic one as such in the fixtures README.
  When baton logs `no limit pattern matched`, that output is exactly the fixture we are missing.
- No sleeps longer than a moment: use the fake clock.
- Do not weaken a test to make it pass; if the behaviour changed on purpose, say so in the CHANGELOG.

## Adding a config key

1. Add it to the allowed key list in `baton/config.py` (`RUN_KEYS`, `PROVIDER_KEYS`, ...) and read it with the
   strict helpers (`_bool`, `_number`, `_opt_str`, ...): a wrong type is a `ConfigError` that names the key, an
   unknown key is an error with a "did you mean".
2. Document it in `docs/configuration.md` (key, type, default, one sentence on why you would change it). The
   docs test (`tests/test_docs.py`) fails when a key the code accepts is missing from that page.
3. Mention it, commented out, in `baton/templates/task-auto/baton.toml` if a new user should know about it.
4. Add tests for the default, a valid value and each way it can be wrong.
5. Add a line to the CHANGELOG; flag a changed default as a behaviour change.

The same goes for task front-matter keys (`baton/tasks.py`).

## Style

- Plain functions and small modules; no new dependencies (the "no runtime dependencies" promise is a feature).
- Fail closed: a check that cannot run blocks, it never passes by default.
- baton never pushes, resets, cleans, rebases (apart from the opt-in `existing_branch = "rebase"`) or stashes.
  Do not add that.
- Messages say what happened, why, and what to do next.

## Releases

See [docs/releasing.md](docs/releasing.md).
