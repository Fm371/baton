# Design: fallback policy and a budget for the run

Status: proposal, no code. Written against 0.3.0 plus the unreleased work.

## Problem

Two real complaints, both about the operator's own quota:

- In one real run, the fallback to Claude drained the operator's weekly Claude allowance (88%). The remote
  session that was watching the run then hit `extra_usage_unavailable`, and the final Claude-only gate risked
  waiting until Friday. Work that would have been fine to wait for Codex ran on Claude.
- Another real project: an overnight run can eat the week's quota for all of the person's work; the weekly limit hit and the
  person was idle for days. Models above the routing table burned a 5-hour window in 40 minutes.

Today the rule is: stay on a provider until it is limited, then use the other one. There is no way to say
"this provider is the expensive one, wait instead".

## Fallback policy per provider

```toml
[providers.claude]
fallback = "allow"            # default: today's behaviour
# fallback = "wait"           # never take a task over from the other provider because it is limited; wait for it
# fallback = { only_for = ["r5", "r6"] }   # fall back to Claude only for tasks on these rungs
```

- `allow`: unchanged.
- `wait`: when the preferred provider is limited, a task waits for it (bounded by `max_sleep_hours`, which
  already ends a task `blocked` when the reset is too far). Tasks restricted by `providers = [...]` are
  unaffected.
- `only_for`: the fallback applies only to the listed rungs; everything else waits. Matches "Opus only for
  money, concurrency, credentials and verification".

This is a filter in `choose_provider` / `select` (scheduler.py), next to `allowed`: a provider that is
`fallback = wait` is excluded for a task unless the task already runs on it, or it is the preferred provider.
Skip-ahead then runs other ready tasks meanwhile, so waiting costs time, not idleness.

`baton plan` prints how many ready tasks would need each provider when the other is limited ("14 tasks, 3 can
only run on claude"), so the operator sees the exposure before the night starts.

## Budget for a run

What the limit messages and CLIs expose decides what can be enforced:

| Signal | Exposed? | Use |
|---|---|---|
| Per-attempt tokens, cache read/create, cost | Yes: Claude's `result` event carries `usage` (already stored in metrics.jsonl) | cumulative tokens/cost per provider for this run |
| Remaining share of the 5-hour window | Only after a limit message names a reset time | not usable ahead of time |
| Weekly allowance used | Claude Code shows it interactively; no stable machine-readable source in `-p` mode (unverified for every version) | not usable ahead of time |

So a budget has to be **spent-in-this-run**, not "left in the plan":

```toml
[run]
budget = { claude_usd = 20, claude_tokens = 50_000_000 }
```

At each task boundary baton adds the attempt's `usage` to a run total; when a provider's budget is exceeded it
is treated like a long limit (no new attempts on it), the log says why, and SUMMARY has a line
`claude: budget used 21.3 of 20 USD`. Running attempts are never interrupted. The reserved-gate idea ("keep
enough for the Claude-only final verifier") becomes `reserve = { claude_usd = 5 }`: other tasks stop using
Claude when the remaining budget falls to the reserve.

Cache reads are ~98% of tokens in the real runs, so tokens are a poor unit; USD (from the usage fields and a
per-model price table the owner supplies, since prices change) or "attempts" are better.

## Why not now

`fallback` is small and safe and could ship first. The budget needs a price table and agreement on units, and
it is only as good as the usage data each CLI prints; Codex's `turn.completed` usage format should be checked
against a real capture before any number is trusted.
