# Design: protected paths with a minimum rung

Status: proposal, no code. Written against 0.3.0 plus the unreleased work.

## Problem

In one real project a non-money task added three read methods to `BillingRepository`, which broke two money-gate tests that pin
that class's public surface. Only the final review found it, and it became a human decision. More generally,
some code (money, credentials, migrations, auth) should only be touched by tasks that were given enough
thinking budget and that someone chose to run with a verifier behind them. A low-rung task has no way to know
it is standing on one.

baton knows which files a task changed (`changed_paths`) and which rung it runs on (`resolved_rung`). It can
connect the two.

## Config

```toml
[repos."../core"]
protected = [
  { paths = ["app/wallet/**", "app/money/**"], min_rung = "r5", why = "money code; gate tests pin its surface" },
  { paths = ["migrations/**"],                min_rung = "r4" },
]
```

Rungs are ordered by their position in `[routing.rungs]` (declaration order), so "at least r5" is well
defined; `plan` fails on a `min_rung` that is not a rung.

## Behaviour

1. **Before** (plan): a task whose `paths = [...]` front matter matches a protected glob and whose rung is below
   `min_rung` is an error in `baton plan`, naming the glob and the reason. That is the cheap, early check.
2. **During** (prompt): the task's prompt lists the protected globs that apply to its repo and says a change
   there is not allowed at this rung; report it as `blocked` instead.
3. **After** (finish): `changed_paths(start_head)` is matched against the globs. A match by a task below
   `min_rung` ends it `blocked:finding` with the file list, and its commits stay on its branch (nothing is
   dropped). Dependants are held by the existing rules for `blocked`. A task at or above `min_rung` passes, and
   SUMMARY marks it `touched protected: wallet` so the reviewer knows where to look first.
4. **Verifier interplay**: a verifier already may only change test paths; protected paths add nothing there.
5. A glob that matches test files too (`tests/wallet/**`) is the owner's call; the default is source only.

## Edge cases

- The check is on the final diff against `start_head`, so a task that touches and reverts a protected file is
  fine.
- Renames: both old and new path count.
- Generated files (lock files, snapshots) under a protected glob make noise; the owner narrows the glob.
- With lanes, `finish()` runs in the worker that owns the repository, so the diff is not mixed with another
  lane's work.

## Why not now

It is simple to build but only worth it with a real list of protected paths per project. The first step with
value is item 3 alone (a post-hoc check reporting `touched protected`), which needs no rung model. Build that
when a project supplies its list.
