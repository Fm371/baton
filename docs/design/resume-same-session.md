# Design: resuming the same session after a limit

Status: proposal, no code. It conflicts with a deliberate property of today's design; read "Conflict" first.

## Problem

Every resume after a usage limit is a fresh session: the agent re-reads the rules, the task, `AGENTS.md`,
the discovery notes and `git log`. In real-run numbers 98% of tokens are cache reads, so cost is roughly
(context size) × (number of steps), and a cold start pays the context again for no new work. For a long task
that hits a five-hour limit once or twice, that is a large share of the bill. A session that continues also
keeps its own reasoning, which the progress file only approximates.

## What exists

- Claude's `result` event carries a `session_id`; baton already stores it in `metrics.jsonl` (`session`).
- `claude -p --resume <session_id>` continues a session. Codex has `codex exec resume <id>` (check the
  installed version; the subcommand and flags differ between releases).
- Handoff today: WIP commit, `results/<id>.progress.md` written by the agent, a prompt that says "run
  `git log` first". It works across providers because it needs nothing from the previous provider.

## Proposal

Resume only when **the next attempt is on the same provider and the same model** as the one that was cut off
by a limit (or a transient error), and the session id is known. Everything else keeps the cold-start handoff.

1. After the attempt that hit the limit, store `ts.session = {provider, model, id}` in the task state.
2. When the same provider becomes available again and `ts.session.provider == prov` and the rung's model is
   unchanged, build the command with `--resume <id>` and send a short prompt ("The usage limit has reset.
   Continue. Re-read your progress file and `git status` first because other work may have been committed.").
3. Fall back to a fresh session, with the full prompt, when: the resume command exits quickly with an
   unknown-session error, the session file is gone (home cleaned), the CLI version has no resume, or
   `resume_same_session = false`.
4. Never resume across a provider switch, a model change, or after an auth error.

## Conflict with today's design

- Today every attempt starts from the same inputs (task text + progress file + git state). That makes
  attempts reproducible and means a stale or poisoned session cannot carry a mistake forward. A resumed
  session keeps whatever the agent believed, including a wrong belief, and baton cannot see inside it.
- Hidden-file rules (verifiers, canaries) are applied per attempt through a settings file. A resumed Claude
  session was started with the old settings; if the deny rules changed between attempts, `--resume` does not
  necessarily pick up the new file. This must be verified before any use with verifiers (it is
  safest to never resume a verifier).
- The prompt for attempt N includes the previous agent's progress notes and "task started before" text;
  a resumed session already has them, so the two prompt paths diverge and both need tests.
- A real mid-task switch to Codex and back failed once on a real night (cause never found) while resuming
  looked fine in fakes. Resume adds another path of that kind: unverified behaviour that fakes cannot prove.

## Recommendation

Do not build it until there is a real capture of one resumed session and the numbers show the cold start is a
material share of cost for the owner's tasks. If built: opt-in per run (`resume_same_session = true`),
implementation-tasks only, Claude only at first, with the fresh-session fallback above and a metrics field
(`resumed: true`) so the saving can be measured against cold starts.
