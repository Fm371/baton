# Limit-message fixtures

One sample per file, `<provider>-<kind>.txt`; `*.stderr.txt` is fed as stderr, the rest as stdout.
Expected outcomes live in `tests/test_limits.py` (`FIXTURES`).

Provenance: these are the message formats the project already recorded in its tests and README
(README "Limits" section); none is captured fresh from a live CLI. `claude-wrap-up`'s resetsAt and
`claude-agent-talks-about-limits` are synthetic shapes built around the recorded text.
When you capture a real message from a run (`results/<task>/attempt-NN.<provider>.out/.err`),
add it here with a one-line row in `FIXTURES`.

`claude-out-of-credits` was reported by the user from a real run (the JSON wrapper around it is the
usual result shape). Credits do not come back on a timer, so it is classified like `auth`: the provider
is disabled for the run and the work moves to the other one. On Codex a depleted workspace comes back at the
reset its rate-limit event names (or hourly when it names none), so `classify_codex` reports it as a `limit`.

Synthetic (built for the structured-evidence checks; shapes follow the recorded events, not a live capture):
`claude-code-usage-limit-no-wording`, `claude-clean-exit-stderr-talk`, `codex-code-insufficient-quota`,
`codex-status-429-no-wording`, `codex-clean-exit-talks-about-limits`. They carry no wording the text grammar
would match (or, for the `*-talk*` ones, appear only where a clean exit makes them the agent's own words), so
the expected outcome comes from the error code, status or exit code alone.
