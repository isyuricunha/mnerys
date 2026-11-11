# M5.3 — Session Working Directory Contract (Increment 1)

One authoritative, owner-scoped, session-scoped working directory per Gateway
session, persisted on the shared session row and honored by every execution
surface. `src/session_cwd.py` is the single documented resolution and
validation point.

## Defaults by client surface

| Surface | New-session default | Never |
| --- | --- | --- |
| Local CLI / local TUI | the invoking shell's actual `pwd` (client-supplied, server-validated); an unusable `pwd` falls back to the execution HOME | a repository root, a generated conversation workspace, filesystem `/` |
| Web | the execution account's HOME | the browser's local path (the browser has no server filesystem access) |
| Telegram (future client) | the execution account's HOME (shared contract; the client itself is M5.3+) | — |
| Remote CLI/TUI | the explicitly selected server directory when supplied and authorized; otherwise the execution account's HOME on the remote host | the client's local `pwd`; filesystem `/` |
| Legacy sessions with no persisted `cwd` | unchanged behavior: no binding, the global tool-path allowlist applies | silent relocation |

A per-conversation workspace remains available as an optional explicit
selection; it is never mandatory and never auto-created for sessions.

## Precedence

1. An explicit per-turn request value the caller is authorized to bind
   (unchanged M2+ behavior).
2. The session's persisted `cwd`.
3. The client-specific default captured at session creation.

When an authorized request binds a directory and the session has no
authoritative CWD yet, the first explicit bind persists as the session's
working directory (first-bind-wins). Afterwards the session keeps its
directory: changing it goes through the validated session API, never through
a silent per-turn relocation.

## Selection and validation

- `POST /api/session` (and the Gateway v1 equivalent) accepts `cwd`; the
  value is validated server-side (`vetted_session_cwd`): real directory,
  not sensitive (`.ssh`, `.gnupg`, …), not deny-listed, not a filesystem
  root, no control characters, bounded length. Invalid selections are
  refused with HTTP 400 and an actionable reason.
- `PATCH /api/session/{sid}` selects a new directory with the same
  validation; an empty value clears the selection (documented); an invalid
  value is refused and the previous directory is kept.
- Non-admin callers get no binding (the workspace-backed tools are not
  available to them; a default directory would be dead state).
- `GET /api/session/{sid}` (and session lists) return the effective CWD.
- Owner checks are unchanged: only the session's owner can read or change
  its directory.

## CWD is not the security sandbox

Binding a directory selects where relative paths resolve and subprocesses
run. It grants nothing: every tool call still passes Gateway admission, the
tool policy, request-authority grants, `vet_workspace` sensitivity checks,
the in-workspace deny lists (`.ssh`, application state, hard-linked files),
resource confinement, and the M5.2 read-before-edit and derived-artifact
publication contracts. No global `os.chdir()` is performed in the shared
Gateway process; subprocesses receive the directory explicitly through the
existing `agent_cwd()` resolution (sealed process root → active workspace →
default agent workspace).

## Tool resolution semantics

- `Read`/`Write`/`Edit`/`Glob`/`Grep`/patch and the inherited filesystem
  tools resolve relative paths against the selected directory; absolute
  paths remain subject to the existing authority and confinement checks.
- `Bash`/`python` and background jobs run with the selected directory as the
  process CWD (the per-turn resource binding re-vets it; a directory that
  disappeared yields a truthful error, never silent execution elsewhere).
- Derived-artifact writers publish into destinations resolved from the
  selected directory under the unchanged publication boundary.
- The model-visible context advertises the real execution directory
  ("Active workspace: `…`"), never a fabricated workspace.
- MCP tools keep their original names and authorization semantics; a CWD
  change never broadens their filesystem reach.

## Approval and concurrency bindings

- A pending exact-operation approval binds the workspace it was sealed for.
  If the session's CWD changes before resume, the sealed match fails and the
  operation is refused — the client requests fresh authorization instead of
  silently redirecting execution (regression-tested).
- Claims remain one-use; denial, expiry, cancellation, replay protection and
  owner/session identity are unchanged.
- Concurrent updates to the same session row follow the existing
  revision/incarnation write-fencing (SESSION_REVISION_MIGRATION).

## Test evidence (increment 1)

`tests/test_session_cwd_contract.py` — 23 deterministic tests: per-surface
defaults (CLI/TUI `pwd`, Web/Telegram/remote HOME, no implicit root, unusable
`pwd` fallback), validation refusals (root, nonexistent, sensitive, symlink
swap, control characters, length), API create/PATCH behavior (validated
selection, invalid refusal keeps the previous value, empty clears, cross-owner
denied, non-admin gets no binding), approval workspace binding and one-use
replay refusal, relative-path resolution, subprocess/search-root resolution,
model-visible context, and deleted-directory handling.

## Remaining M5.3 work

Four-choice approval UX (allow once / session / scoped rule / deny), the
deterministic command-risk classifier, opt-in YOLO mode, the Chat/Agent UI
unification, and the hosted verification of the exit gate (risky-command
prompts, all four decisions, YOLO/deny/owner-boundary negatives).
