# Native Git Hooks — Mnerys

Mnerys uses **native Git hooks**, with no hook manager or additional
runtime dependency. The version-controlled hooks in [`.githooks/`](../../.githooks/)
call the repository-specific [validation driver](../../scripts/dev/git_validation.py).
They intentionally reuse commands from [CI](../../.github/workflows/ci.yml)
instead of defining a second test suite.

## Activation

After cloning, from the Mnerys repository root:

~~~sh
git config --local core.hooksPath .githooks
~~~

This activation is **local to the clone** and is not transmitted by Git.
Git executes the checked-in `pre-commit` and `pre-push` files because they
are executable. Never configure a global `core.hooksPath` for this project.

For Python tests, the driver first checks `venv/bin/python`, then
`.venv/bin/python`, then the current Python interpreter. On installations
using an external virtualenv, configure its interpreter **locally**:

~~~sh
git config --local mnerys.hooksPython /path/to/your/venv/bin/python
~~~

The configured interpreter must already have the application's pytest
dependencies. Hooks never install packages, alter the environment or start
containers. The local interpreter selection is not version-controlled.

## What runs

**`pre-commit` (fast):** Git's staged whitespace/error check; Python
syntax compiled from **staged blobs**, not working-tree replacements;
`node --check` for staged JavaScript and module files; shell syntax for
staged shell scripts and hooks. If frontend or builder sources changed,
it also runs the existing `scripts/build_frontend.py --check`. Staged
checks use only stdlib Python, Node and Git; they never run background
services. Failed checks abort the commit.

**`pre-push` (comprehensive):** requires a clean working tree to test the
exact commit to be published. Reuses the maintained CI commands:
Python `compileall`, committed frontend build check, frontend
architecture check, Node syntax across maintained source modules,
Node architecture and semantic-release tests, then **all four**
`pytest --shard N/4` jobs with the CI's `-q -rs --durations=25` options.
Every shard is attempted so failures can be diagnosed together; any
failure blocks the push. No `--no-verify`, automatic repair, test
deselection or fallback to a partial suite.

The test driver uses the interpreter configured locally, or a suitable
existing project environment. If no usable Python/pytest or Node
environment is available, **pre-push fails closed** and explains what is
missing.

## Local checks versus hosted CI

These hooks reproduce the runnable *test commands*, **not the entire
GitHub-hosted runner**. CI still provides a pinned Node 22/Python 3.11
environment for main tests, browser installation, FFmpeg, functional
bubblewrap namespaces/AppArmor, CodeQL, dependency/secret scans and
image builds. On an environment with different operating-system
capabilities, the same test command may fail or exercise less than CI.
Do not call local checks a full CI pass when those prerequisites differ.

PostgreSQL 17 integration is tested with isolated disposable databases
when appropriate; the hook must **never** connect to a real Mnerys
production database or automatically start a database/container. The
separate [PostgreSQL workflow](../../.github/workflows/postgresql17.yml)
and [M2.1 evidence](../architecture/M2_1_EXIT_GATE.md) still govern that
acceptance.

To test the same commands manually, without committing or pushing:

~~~sh
python3 scripts/dev/git_validation.py pre-commit
python3 scripts/dev/git_validation.py pre-push
~~~

The first checks the **current Git index**; the second needs a clean
working tree. All failures must be fixed, not bypassed. The hooks are
local prevention, not replacements for GitHub branch protection or CI.

## Maintaining the hooks

Keep the command list aligned with
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).
When CI tools/versions or sharding change, update the validator and its
[regression tests](../../tests/test_mnerys_native_git_hooks.py) together.
Preserve existing hooks if another clone already has custom activation;
inspect its `core.hooksPath` before replacing it.
