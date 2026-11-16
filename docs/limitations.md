# Known limitations

What baton does not do yet, or does only partly. The short version is in the [README](../README.md).

- **Agents are limited only by the tool's settings or sandbox** ([safety.md](safety.md)). baton does not
  sandbox the agent itself. `results/` is writable by every agent, so a task can in principle read or
  overwrite another task's result files.
- **Experimental features.** `lanes > 1` ([lanes.md](lanes.md)) and `return_mid_task` are off by default.
  Try them on a low-risk night before relying on them.
- **The reviewer's read-only guarantee has a gap.** `baton review` denies writes through Claude's settings
  and Codex's read-only sandbox, and compares the repository before and after. A command that an
  interpreter hides from the deny rules is caught only by that comparison, and a content change to an
  untracked file that keeps its size and time is not seen.
- **A limit message baton cannot read** is handled with a cooldown and a `no limit pattern matched` warning,
  not a reset time. Send the saved output (`state/fixtures/`) so it can be added to `tests/fixtures/limits/`.
- **Pre-existing test failures are not tracked per task.** Only `repo-suite` has a baseline, so an unrelated
  failing test can make later tasks report `partial`. A design for a per-task baseline is in
  [design/baseline-failures.md](design/baseline-failures.md); it is not built.
- **Not built, design only:** resuming the same agent session after a reset
  ([design/resume-same-session.md](design/resume-same-session.md); every resume is a fresh session with the
  previous agent's notes), a per-run budget and fallback policy, protected paths per repository, and two
  tasks in the same repository at once. Proactive wrap-up before a limit and escalation to a higher rung do
  not exist either.
- **Logs and raw agent output are never rotated** ([operations.md](operations.md#housekeeping)). A very
  large `.out` file is read from its tail, but still sits on disk until you prune it.
- **One run per folder.** A second `baton run` in the same workspace refuses to start.
- **Linux is the tested platform.** macOS should work (baton uses POSIX calls only) but is untested.
  Windows is unsupported.
