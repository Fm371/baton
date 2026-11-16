# baton documentation

Start with the [quick start](quickstart.md). The rest is reference: read it when you need it.

| page | read it when |
|---|---|
| [quickstart.md](quickstart.md) | you have never run baton: install, create a workspace, run one task on a throwaway repository |
| [concepts.md](concepts.md) | you want the model in your head: the workspace, the switching rule, rungs, what baton never does |
| [tasks.md](tasks.md) | you are writing task files: front matter, the status table, branches, the WIP secret filter, `## User journey` and `## Reuse`, approval checkpoints |
| [verifiers.md](verifiers.md) | you want an independent check of an agent's work, or the built-in ones: env-preflight, browser-smoke, real-data-smoke, repo-suite, the hardcoded-key scan, the Not verified section |
| [configuration.md](configuration.md) | you need to know what a key does, its type and its default: `[run]`, `[providers.*]`, `[routing]`, `[services.*]`, `[preflight]`, `[repos]`, `[plan]`, task front matter |
| [operations.md](operations.md) | you run it overnight: commands, the launch checklist, how to know the night went wrong, exit codes, reading the summary, limit detection, housekeeping, metrics |
| [environment.md](environment.md) | tests need a database, the network or a variable: env-limited results, services, notifications, required variables, paths, the Codex sandbox |
| [safety.md](safety.md) | before the first real night: what baton enforces itself and what it leaves to the tools |
| [lanes.md](lanes.md) | you want tasks in different repositories to run at the same time (experimental) |
| [troubleshooting.md](troubleshooting.md) | something failed: `bwrap`, network, a suite that is "not green", a stale lock, a limit message baton cannot read |
| [limitations.md](limitations.md) | you want to know what baton does not do yet, before you rely on it |
| [development.md](development.md) | you change baton: tests, the fake CLIs, adding a limit-message fixture |
| [releasing.md](releasing.md) | you cut a release: version, changelog, build, tag, what is not automated |
| [design/](design/) | proposals that are not built: parallel lanes (built, kept as the rationale), baseline failures, fallback and budget, protected paths, resuming the same session |

The project overview, and the short start are in the [README](../README.md); every
user-visible change is in the [CHANGELOG](../CHANGELOG.md).
