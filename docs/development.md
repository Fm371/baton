# Development

```bash
pip install -e '.[dev]'                         # adds ruff
python3 -m unittest discover -s tests -t .      # all tests, fake CLIs only (about 5 min)
python3 -m tests.run_parallel                   # same, one process per test module (about 100 s on 4 cores)
ruff check baton tests
```

## How the tests work

Fake `claude` and `codex` executables (`tests/fake_agent.py`) replay scripted outputs against real git
repositories, with a fake clock, so a two-day weekly wait takes milliseconds. Tests are plain `unittest`, one
module per area, with no network and no real agent.

They check baton's own logic, not the real tools: the message formats baton reads (usage limits, errors,
`stream-json` events) are copied from recorded samples in `tests/fixtures/limits/`, and **how the real CLIs
behave is unverified** beyond what real runs have shown.

## When a real run shows an output baton did not expect

1. Take the saved output: `results/<task>/attempt-NN.<provider>.out` / `.err`, or the sample baton keeps in
   `state/fixtures/` when it meets a message it does not recognise.
2. Check what baton makes of it: `baton classify claude results/10-example/attempt-03.claude.out --exit-code 1`
   (or `codex`, with `--timezone` for reset times printed without a zone).
3. Add it as a fixture in `tests/fixtures/limits/` with a row in `FIXTURES` in `tests/test_limits.py`, then
   change `baton/limits.py` until the row passes. Remove secrets from the sample first.

## The documentation is tested

`tests/test_docs.py` fails when a key of `baton.toml` or of a task header is missing from
[configuration.md](configuration.md), when a `baton` subcommand is documented nowhere, when a relative link in
the README or `docs/` points at a file that does not exist, or when the README grows past 250 lines. Add a
config key, add a row to the reference in the same change.

## Platform

Linux is the tested platform. macOS should work (baton uses only POSIX calls, and `caffeinate` replaces
`systemd-inhibit`) but is untested; Windows is unsupported.
