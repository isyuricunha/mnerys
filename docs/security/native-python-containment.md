# Native Python process containment

The native `python` tool uses the same admitted execution and process
containment service as other native subprocess tools. On Linux with
Bubblewrap available, its root filesystem exposes only the system mounts,
the authorized workspace and explicitly admitted read-only runtime/package
paths. It does **not** bind the entire host home directory or silently fall
back to unconfined execution.

## Python binary selection

Virtual environments can be located outside the mounted system directories,
and their executable paths can contain symlinked ancestors. The shared
native subprocess spec already admits the canonical Python environment
prefixes as read-only mounts. After acquiring a Bubblewrap grant, the
Python tool resolves the trusted base executable through
`sys._base_executable` and requires its canonical path to be under the
already mounted `/usr` tree or the canonical `sys.base_prefix`.
A missing, untrusted, or non-executable binary fails closed. No additional
mount is created by this selection.

Non-Bubblewrap execution retains its existing interpreter selection.
Explicit opt-in Python site-package roots remain governed by the inherited
read-only `ODYSSEUS_PYTHON_TOOL_SITE_PACKAGES` contract.

The end-to-end containment checks use the same canonical base executable
when testing raw `containment.run` directly. The tests still verify the
real process boundary, network isolation, namespace teardown, timeouts and
workspace confinement rather than substituting mocks for them.

## Ubuntu AppArmor prerequisites

On Ubuntu hosts that restrict unprivileged user namespaces, Bubblewrap
requires a suitable installed and loaded AppArmor profile. Prefer the
distribution-maintained, narrowly scoped profile for Bubblewrap; do **not**
disable AppArmor globally or set the global user-namespace restriction to
zero merely to run the tests.

After an authorized administrator configures the profile, verify a
read-only `bwrap` namespace probe and run the native `pre-push`
validation. The latter runs the maintained CI-like four-shard pytest suite,
Python checks and frontend checks. Hosted CI and its security scanners are
still independently authoritative.

## Acceptance

- [Python binary confinement regression](../../tests/test_mnerys_native_python_sandbox_binary.py)
- [Workspace process tests](../../tests/test_workspace_confine.py)
- [Native containment contracts](../../tests/test_native_execution_containment.py)
- [Namespace enforcement and lifecycle](../../tests/test_containment_enforcement.py)

This does not change the Gateway approval policy, resource authority or
live deployment. Runtime-specific package availability and non-Linux
isolation mechanisms remain separately tested.
