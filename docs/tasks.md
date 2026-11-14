# Tasks, branches and checkpoints

## Task files

```markdown
+++
repo = "../my-repo"        # relative to baton.toml, or absolute
work_type = "crud"         # a key of [routing.work_types]   (or: rung = "r3")
branch = "feat/widgets"    # created from base if missing
base = "dev"               # optional; overrides run.default_base for this task
providers = ["claude"]     # optional; only these providers run it (no fallback)
depends_on = ["05-schema"] # optional
priority = 10              # optional; higher first among ready tasks
timeout_min = 60           # optional
max_turns = 80             # optional, Claude only
feature = "widgets"        # optional; tasks sharing a feature get an env-preflight after the last one
paths = ["src/app/widgets/**"]   # optional; extra paths the task touches (see below)
lane = "shared-db"         # optional; with run.lanes > 1, tasks in one lane never overlap (see lanes.md)
+++
## Goal …
## User journey …
## Reuse …
## Acceptance …
```

Every front-matter key, with its type and default, is in [configuration.md](configuration.md#task-front-matter).
Files are read in name order; `_draft.md` and `README.md` are skipped. Each
task ends with the agent writing `results/<id>.md` whose first line is
`STATUS: done|partial|blocked`. baton decides the final status itself:

| agent says | commits on the branch | final |
|---|---|---|
| done | any | done (a note if zero) |
| partial | ≥ 1 (a WIP commit that baton made counts) | partial |
| partial | 0, even after baton committed leftovers | **failed** |
| (nothing) after retries | – | failed |

Dependants run only after `done` or `partial` (after a verifier: only `done`).

A task id is the file name without `.md`, or `id = "..."` in the front matter. It may use only ASCII
letters, digits, `.`, `_` and `-`, and must start with a letter or digit (the same goes for `lane`);
it becomes part of branch and file names.

### Branches

- A task with no `branch` works on `baton/<id>`, created from `base` (else `run.default_base`, else
  the checkout the repository is on when the task starts). baton never commits on your current branch.
- A task that depends on a task in the same repository starts from *that task's* branch, so it sees the
  work it builds on. This applies when the two branches differ and the file has no `base`. With
  dependencies on several different branches, set `base` yourself (`baton plan` warns).
- **A branch that already exists is used as it is, so baton checks it.** An old `feat/*` branch from an
  earlier night may lack the work of the task the new one depends on, or have nothing in common with
  the base; building on it silently would run the task without its dependency. `baton plan` shows
  `[branch exists: N commits over <base>, M behind it]` and warns about a branch that does not contain
  the branch of a task it depends on (or has no common history with its base). At run time
  `[run] existing_branch` decides: `"fail"` (default) refuses the task with the reason, `"warn"` goes on
  and logs it, `"rebase"` rebases the branch onto the dependency's branch (it is the one place baton
  rebases; a conflict aborts the rebase and the task is refused). A branch that is merely behind its
  base is only reported. A task that continues its own interrupted branch is not checked again.
  This also keeps a verifier from testing stale code: a `verify/<id>` branch left from an earlier night
  must contain the branch it checks.
- When the run ends every repository is put back where it was before baton touched it: on its branch, or
  for a detached HEAD on the same commit (`baton plan` says which). A repository with uncommitted changes is
  left alone, and the log says where. A branch an agent switched the repository to is never taken for
  yours: baton keeps the branch it recorded and returns to it.
- `baton plan` warns when a task's `branch` is `main`, `master`, `run.default_base` or the branch checked
  out in the repository right now: the task would commit straight onto it.
- A refusal because of uncommitted changes lists the first few files from `git status --short`. A missing
  `base` says how to create it (`git branch <base> origin/<base>` when the remote branch is known
  locally; baton never fetches). A bare repository is named as one.
- A WIP commit (`chore(<id>): wip, ...`) has two tiers of secret-looking files, both lists of file-name globs
  (a glob with a `/` is matched against the whole path from the repository's top folder; case is ignored):
  - **Refused** (`[run] wip_secret_patterns`; default `.env`, `.env.*` but not `.env.example`/`.sample`/
    `.template`/`.dist`, `*.pem`, `*.p12`, `*.pfx`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`,
    `id_dsa*`, `.pypirc`, and any file that starts like a private key): never committed. An untracked one is
    added to `.git/info/exclude` so the repository stays clean and the queue goes on; the task note lists it.
  - **Warned** (`[run] wip_warn_patterns`; default `*.key`, `credentials*`, `secrets.*`, `.npmrc`, `*.token`,
    `*.secret`, `service-account*.json`, `.netrc`, `.htpasswd`): names that are often ordinary code or data,
    so they **are committed** and the queue never stops on a false alarm. Every one is reported loudly: a
    `WARNING` log line per file, the task note (`committed secret-looking files: ...`, which is also the
    `on_event` payload), and a `## Warnings` block at the top of `SUMMARY.md`.
  - The trade-off is yours to set: a refused file leaves its work uncommitted (a file the agent needed may be
    missing from the branch), a warned file may land in history. If a warned file really is a secret, remove
    it before anything is pushed, and consider a secret scan (for example `gitleaks`) as a pre-push check on
    the task branches. `[]` switches a tier off; a list replaces its default. A tracked file that changed
    cannot be kept out of a commit by name, so the refuse tier only covers new and untracked files.
  A WIP commit also warns about files over 20 MB. If it fails (no git identity, a lock file), the log says so
  and the next task in that repository fails with that reason.
- Keep the task-auto folder out of the repositories it works on, or have git ignore `results/`, `state/`
  and `logs/`: otherwise `baton plan` refuses to start, because those folders would look like uncommitted work.
- `baton run --only <id>` on a task that ended starts it over: counters reset and the old
  `results/<id>/attempt-*` files move to `results/<id>/earlier-N/`.

## User journey and Reuse

Acceptance criteria written as API and money behavior pass on a feature that
is the wrong feature. Two optional sections state the intent a test cannot:

```markdown
## User journey
The applicant opens the invite link, logs in, and then sees exactly the normal
invitation page, data form and payment page. The link is only an entry path.

## Reuse
The existing invitation flow under src/app/(individual)/invitations/. Reuse
its pages; do not build a standalone join page.
```

- `## User journey`: what the person does and sees, step by step.
- `## Reuse`: which existing page or flow this resembles and must reuse.
- `baton plan` warns about a task that touches UI paths and has neither
  section. A task touches a UI path when its text names a file that matches
  `plan.ui_paths` (default `src/app/**` and `*.tsx`; a pattern without a slash
  matches the file name at any depth) or its `paths` list does:

  ```toml
  [plan]
  ui_paths = ["src/app/**", "*.tsx", "web/pages/**"]
  ```
- `common.md` tells the agent to look for the closest existing flow before it
  builds a new one, to reuse it or say in the result file why not, and to walk
  the journey before it reports done.

## Asking for a review

`review = true` in a task's header opts it in to `baton review --pending` (default `false`). It changes
nothing about how the task runs: reviews and fixes happen only when you run `baton review` /
`baton review-fix`, never from `baton run`. See [operations.md](operations.md#cross-provider-review).

## Approval checkpoints

A task with `kind = "approve"` is a human checkpoint. It needs no `repo` and
runs no agent; its body holds the open questions.

```markdown
+++
kind = "approve"
depends_on = ["30-join-page"]
+++
Does the join flow match the normal invitation → data form → payment flow?
Is the theme the same as the other pages?
```

When it becomes ready baton writes `results/<id>.md`: your questions, the
journey and reported result of each task before it, and the screenshots saved
under `results/<task>/`. The checkpoint is then **awaiting**, and every task
that depends on it, directly or not, waits. Tasks without such a dependency
keep running. Then:

- `baton approve <id>` releases the dependents (run `baton run` again if the
  run already ended; a run that is still busy picks the decision up itself).
- `baton reject <id> --note "..."` marks the dependents **blocked**, with the
  note, and the checkpoint stays rejected until it is approved.

Decisions are files under `state/decisions/`; only the runner writes the run
state. `baton run --only <task>` refuses a task behind an open checkpoint
(exit code 5) unless `--force` is given.
