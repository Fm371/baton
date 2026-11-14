# Rules for every task in this run

You are one step of an unattended run. Nobody will answer a question until
the morning, so never ask: take the most reasonable reading, record it under
"Decisions made" in your result file, and carry on.

## Git
- The runner has already put your repository on the task's branch. Commit
  there. Do not create, switch, merge, rebase, reset or stash branches.
  Never push.
- Commit in small, meaningful commits as each part goes green. Never commit a
  failing test or a broken build.
- Before you start, run `git log --stat <base>..HEAD`. If part of the task is
  already there, don't redo it: check it, fix what is wrong, and say so.
  A commit named `chore(<task>): wip, ... (unverified)` is unfinished work from an
  earlier attempt (maybe by another agent): finish it or replace it.

## Work
- Run every command in the foreground and wait for it. Your process ends when
  your reply ends, so a background job never reports back.
- Tests first where there is logic. During the task run only the new or
  changed test files; run the full suite, lint and typecheck once, at the end, with the commands your
  settings allow (the example allows `pytest`, `ruff check`, `mypy`, `npm test`,
  `npm run lint`, `npm run typecheck` and similar; if a check you need is not
  allowed, say so under "Issues" instead of retrying it).
- If the same check fails after three honest attempts, undo just that part by editing
  the files back (`git checkout`, `git restore` and `git reset` are not available to you),
  record it under "Issues", and continue with the rest.

## Reuse before you build
- Before building a new page, flow or endpoint, find the closest existing one
  (search the routes, components and endpoint modules) and reuse it. If the
  task has a `## Reuse` section, that is the reference. If you still build
  something new, say why in the result file under "Reuse".
- If the task has a `## User journey`, the finished work must let a person
  follow exactly that journey in the running app. Walk it step by step before
  you write the result file; passing unit tests is not the same thing.

## Data keys
- Never hardcode a key that names a record in the database: a resource type,
  plan kind, role, tenant. Test fixtures create the key your code expects; a
  real database names it differently and the endpoint fails there. Resolve the
  key from config or from the database. If a literal really is a constant of
  the protocol, say why under a `## Hardcoded keys` heading in the result file.
  baton scans the lines you added and sends you back once for each literal
  that looks like a data key and is not explained.

## Error messages
- Never show a user a generic error without an error code. "This is not
  available, try again" hides every cause; say what failed, with the error
  code or the HTTP status.
- A 422 says which field is wrong and what format is expected
  ("email: must look like name@example.com").
- An unknown error shows the HTTP status.

## Keep context small (every step re-sends it)
- Read each file once. Use `grep -n` to find the part you need, then read just that
  range with the Read tool (`offset` and `limit`).
- Pipe every test, lint, typecheck and build through `tail -n 40`; rerun with
  full output only for a failing file.
- You may use read-only helper agents on a cheap model for searching and
  summarising; every edit stays with you.

## Result file
Write it last. First line exactly one of `STATUS: done`, `STATUS: partial`,
`STATUS: blocked`. Then short sections: Changed (commits), Criteria (one line
per acceptance item with the test or command that proves it), Reuse (what you
built on, or why not), Decisions made, Issues, Commands. No pasted logs.
