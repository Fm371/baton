# Security

## Reporting a problem

Please report a vulnerability **privately**: use GitHub's "Report a vulnerability" (Security tab →
Advisories) on https://github.com/Fm371/baton, or contact the maintainer directly. Do not open a public
issue for it. Include the baton version (`baton --version`), what you did, and what happened. Expect an
acknowledgement within a few days; this is a small project and there is no bounty.

## What baton does not protect you from

baton runs coding agents unattended **with write access to your repositories**. Read the "Safety" section of
the README before the first night. In short:

- **baton is not a sandbox.** It starts `claude` and `codex` with the settings and sandbox flags you
  configure; those tools, and the permissions you give them, are the only boundary. The example
  `claude-settings.example.json` is a starting point, not a guarantee: deny rules match command prefixes, so an
  allowed interpreter can do things the deny list names.
- **Run it as a separate user or in a container**, on a machine whose credentials you can afford to expose to
  a misbehaving agent (no production cloud keys, no push access you would regret).
- **`results/` is writable by every agent**, so one task can read or overwrite another task's result files.
- **Secrets can reach git history.** WIP commits refuse certain files (`.env*`, private keys), but files that
  only *look* secret (`*.key`, `credentials*`, `secrets.*`, `.npmrc`, ...) are committed with a warning so the
  queue never blocks. Scan before you push (baton itself never pushes).
- **Hooks and `on_event`** run commands you configure; treat `baton.toml` like a script.
- Codex has no read restriction on the workspace; a verifier's hidden files are visible to a Codex task that
  runs in the same repository.

baton itself never pushes, resets, cleans or stashes, runs no server and sends no telemetry. (It does open
local TCP connections when you ask it to health-check a service or probe a sandbox.)
