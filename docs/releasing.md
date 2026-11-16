# Releasing baton

baton is published from `main`. The distribution name is `baton-runner`, the command is `baton`.
Publishing a GitHub release uploads the package to PyPI (`.github/workflows/publish.yml`); the steps before it are run by a person.

## Before you start

- `main` is green: CI (ruff, the quality budget, the tests on Python 3.11–3.13, the build job) passes.
- If you can, run one real night with `lanes = 1` on a few small tasks with the version you are about to
  release. The test suite uses fake CLIs only (see CONTRIBUTING.md), so a real run is the only proof that the
  real `claude` / `codex` still behave the way baton expects. Add every message baton did not recognise to
  `tests/fixtures/limits/` first.

## Steps

1. **Version.** Bump `version` in `pyproject.toml` (`tests/test_version.py` checks that the package, the
   changelog and `baton --version` agree).
2. **Changelog.** Rename the top section of `CHANGELOG.md` from `## X.Y.Z — not yet released` to
   `## X.Y.Z (YYYY-MM-DD)`. It must stay the first `## <version>` heading; keep the **Behaviour changes**
   paragraph at the top of the section accurate (changed defaults, new errors, renamed keys). Start the next
   section (`## X.Y.Z+1 — not yet released`) when the next change lands.
3. **Local build and check.**
   ```bash
   rm -rf dist build *.egg-info
   python -m build                 # sdist and wheel into dist/
   twine check dist/*
   ```
   Install the wheel in a fresh venv and try it: `baton --version`, `baton init /tmp/ws`, `cd /tmp/ws && baton plan`
   (the plan will complain about the missing agents and the example repository; it must not crash).
   Look inside: the wheel has `baton/templates/task-auto/…` and `baton/checks/browser_driver.js`, the sdist has
   `LICENSE`, `CHANGELOG.md`, `README.md`, `docs/`, `tests/`, `tools/`.
4. **Quality budget.** `python tools/quality_budget.py` passes. If you shrank functions since the last release,
   run it with `--update` so the gain is locked in.
5. **Commit and tag.**
   ```bash
   git commit -am "chore(release): X.Y.Z"
   git tag -a vX.Y.Z -m "baton X.Y.Z"
   git push origin main vX.Y.Z
   ```
6. **GitHub release.** Create a release from the tag and paste the changelog section as the notes. Publishing
   it starts `.github/workflows/publish.yml`: it checks that the tag is `v` + the `pyproject.toml` version,
   builds and checks the sdist and wheel, installs the wheel, and uploads to PyPI through Trusted Publishing
   (no API token). The job runs in the `pypi` environment; the PyPI side is configured once for project
   `baton-runner`, owner `Fm371`, repository `baton`, workflow `publish.yml`, environment `pypi`.
7. **Check.** `pipx install baton-runner==X.Y.Z` in a clean shell, then `baton --version`.

## License

baton is MIT-licensed (`LICENSE`, `license = "MIT"` in `pyproject.toml`). The copyright line currently reads
`Copyright (c) 2026 Fm371`, the GitHub account name; change it in `LICENSE` if you want your name or an
organisation there. The build needs setuptools 77 or newer for the SPDX `license` field (pinned in
`pyproject.toml`).

## What is not done

- CI cannot prove anything about the real `claude` / `codex` CLIs (fake CLIs only); there is no scheduled job
  that runs them.
- Only Linux is supported and tested; macOS is untested.
- `docs/design/*` notes (baseline of existing test failures, fallback and budget, protected paths, resuming a
  session) are designs, not features.
