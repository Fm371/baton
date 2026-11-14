# Design: judge a task only on new test failures

Status: proposal, no code. Written against 0.3.0 plus the unreleased work.

## Problem

Real runs: one task left a failing test behind (a test bug, not a code bug). Every later task then ended
`partial` because "the full suite is not green", although none of them had a problem of its own: T06, T07, T08 and
two verifiers in one real run. A person reading `partial` cannot tell "this task is unfinished" from
"this task inherited someone else's red test". The agents' own reports are no help: "pre-existing" was
taken on their word, and in one real run 34 mypy errors called pre-existing were new.

`repo-suite` already does this for the one full-suite check (`suite_baseline` file, only new failures count).
Agent tasks have nothing similar.

## What a task should be judged on

The set of failing tests when the task **ends** minus the set when it **started**. A failure that is in both
is inherited and is reported next to the verdict, never inside it.

## Options

1. **Baseline file per repo** (exists for `repo-suite`). Cheap and explicit, but it goes stale and somebody
   has to maintain it. Good for a known, accepted red set; bad for "the previous task just broke one".
2. **baton measures at task start** (recommended). With `[repos."<path>"] suite_cmd` and
   `suite_failure_regex` (already parsed for `repo-suite`), baton runs the suite on the task's branch before
   attempt 1 and stores the failing ids in `state` (`baseline_failures`). At the end it runs the suite again
   and computes new = end − start. Cost: two full runs per task; the start run can be skipped when the
   previous task of the same repo ended with the same HEAD (reuse its end result).
3. **Ask the agent** to run the suite at start and end and write both lists in the result file. Zero baton
   cost, but it is exactly the self-report that failed in the runs above.

## Recommendation

Option 2, opt-in per repository (`baseline = true`), reusing `suite_cmd` and `suite_failure_regex`.

- A task whose agent said `done` but whose end run has **new** failures is downgraded to `partial` with the test
  ids in the note (this also catches the agent who stopped before the suite finished).
- A task whose agent said `partial` only because of inherited failures keeps its verdict, with
  `inherited N failure(s)` in the note and a `baseline` line in SUMMARY. Whether that should upgrade it to
  `done` is a separate decision: keep it as a note first.
- Failures that are `env-limited` (see `baton/envlimit.py`) in the end run are excluded from "new"; a suite that
  cannot run in the sandbox is not a regression.
- Verifiers are exempt: they add tests on purpose.
- The owner of an inherited failure is the task whose end run first showed it; SUMMARY can name it
  (`inherited from T05`). A later `baton retry` of that task refreshes the baseline.

## Open questions

- The suite can take an hour (`suite_timeout_min`). Per-task runs may cost more than the tasks. A `scope`
  (changed test files only) is cheaper but then proves less.
- Flaky tests (`rerun_failures = 1`: a failure that passes on rerun is `flaky`, not `new`) belong to the
  same feature; without it the baseline is noisy.
- Two lanes working in one repository never overlap, so a lane's start/end runs are not disturbed by another
  lane's edits (lane exclusion is by repository).
