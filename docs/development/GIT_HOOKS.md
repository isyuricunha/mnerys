# Native Git Hooks — Mnerys

Mnerys uses native Git hooks, without external hook managers or added
runtime dependencies. The version-controlled [`.githooks/`](../../.githooks/)
directory contains **only the fast `pre-commit` hook**, which calls
[`scripts/dev/git_validation.py`](../../scripts/dev/git_validation.py).

The full CI-like test suite remains available explicitly and in the
maintained [GitHub Actions CI](../../.github/workflows/ci.yml).
It is **not invoked automatically by `git push`**. Running the four pytest
shards three times for GitHub, GitLab and Forgejo caused unacceptable delays.

## Activation

After cloning, from the Mnerys repository root:

```sh
git config --local core.hooksPath .githooks
```

This config belongs to the local clone; Git does not transmit it.
The repository must not configure a global `core.hooksPath`.

## Fast pre-commit

The `pre-commit` hook checks:
- Staged whitespace with `git diff --cached --check`.
- Python syntax from the exact **staged blobs**, not unstaged replacements.
- JavaScript and shell syntax for staged files.
- The existing frontend build check when relevant frontend or builder
  files changed.

These failures block the commit. There is no automatic repair, Docker
startup, dependency installation or network access.

## Full validation (explicit only)

To run the available CI-like commands when needed:

```sh
python3 scripts/dev/git_validation.py full
```

This requires a clean working tree and runs Python `compileall`, the
frontend build and architecture checks, Node syntax/release tests and **all
four pytest shards** with the current CI options. It is intentionally
opt-in; it does not block or run on `git push`.

For an external test virtualenv, the validator supports an existing
local-only interpreter selection:

```sh
git config --local mnerys.hooksPython /path/to/your/venv/bin/python
```

The interpreter must already have the project's pytest dependencies.
The validation driver does not install packages or start containers.

The full command approximates the runnable CI commands, **not the hosted
runner**. GitHub CI also provides its own Node/Python environment,
browser tools, Bubblewrap/AppArmor prerequisites, security scanners and
image jobs. Isolated PostgreSQL 17 tests are covered by the separate
[PostgreSQL workflow](../../.github/workflows/postgresql17.yml) and
[M2.1 acceptance evidence](../architecture/M2_1_EXIT_GATE.md). No local
test may use the production database.

## Maintenance

Keep the opt-in validator's commands aligned with
[`ci.yml`](../../.github/workflows/ci.yml) and update
[hook regression tests](../../tests/test_mnerys_native_git_hooks.py)
when the contract changes. CI remains the authoritative full regression
check for pushed commits. Do not silently skip failing tests when
reviewing CI results.
