# M5.3 — Secure Opt-In YOLO Mode (Increment 4)

YOLO is an explicit, authorized, SERVER-OWNED session execution mode that
reduces ordinary approval interruptions while preserving every security
boundary. Default: OFF for every new session. There is no environment-variable
activation, no global flag, and no model-driven toggle.

## Semantics: what YOLO suppresses, and what it never touches

YOLO may suppress **category 1 only** — a discretionary prompt for an
operation that is already authorized by the active request authority and
current policy:

| Category | Example | YOLO behavior |
| --- | --- | --- |
| 1. Authorized + discretionary prompt | risk-classifier `REQUIRES_APPROVAL` for an in-workspace command; the external-untrusted-context gate for understood tools | **suppressed when YOLO is enabled** (classification still runs and is audited) |
| 2. Denied by policy / request authority | denylisted tool; operation outside the granted resources | **remains refused** |
| 3. Mandatory human confirmation | delegated spending limits; sealed replay settlement | **still required** |
| 4. Non-delegable security control | delegated-credential workspace blocks; the risk classifier's `DENIED` category | **remains refused — no approval lifts it** |

Explicitly preserved with YOLO ON: Gateway authentication, owner/session
checks, tool admission, explicit policy denials, request-authority grants,
filesystem confinement and OS permissions, read-before-edit baselines,
derived-artifact publication protection, resource/CWD binding, unknown/high-
impact tool refusals under external context (fail closed), auditing,
cancellation, replay protection, and the four-decision approval flow itself.

YOLO is not `return True`: the deterministic risk classifier classifies every
relevant command even when YOLO is enabled (audit evidence stays truthful),
`DENIED` classifications refuse, and the suppressed decision is logged with
the run, tool, level and reason — without recording flag/secret content.

## Activation and lifecycle

- **State**: `sessions.yolo_enabled` (server column, default OFF) — migrated
  idempotently (`ALTER TABLE … DEFAULT 0`); never inherited by new sessions.
- **Toggle**: `POST /api/v1/sessions/{sid}/yolo` with
  `{enabled: bool, confirmed: bool, expected_revision?: int}`. Enabling
  REQUIRES `confirmed: true` in the same request — the user's explicit
  acknowledgment that ordinary approval prompts may be skipped. Selecting
  Agent mode, opening a project, using a model, resuming a conversation, or
  ANY model-generated text never activates it (no tool exposes a toggle).
- **Revision fencing**: a stale `expected_revision` is refused (409);
  concurrent toggles resolve deterministically; disabling is always allowed.
- **Persistence**: the state survives reconnects and restart recovery (the row
  is the source of truth); `GET /api/v1/sessions/{sid}` returns the effective
  state + revision; the session list carries it for client sync.
- **Isolation**: owner-checked admission; another owner or an unrelated
  session can never read or flip it. Cloned/imported sessions start OFF.
- **Revocation**: disabling takes effect immediately for subsequent
  operations; a previously denied/expired/canceled/consumed approval is NOT
  revived by toggling (the pending is already consumed).

## Wiring

`stream_admitted_agent_turn` reads the state from the ADMITTED SESSION row
(`yolo_enabled` is a protected server-owned field — callers cannot inject it
through agent options) and threads it into the loop's
`ToolRunSecurityContext`. The legacy `PATCH /api/session` surface exposes the
same state (`yolo` + `yolo_confirm` form fields, confirmation required to
enable, authorized accounts only). All clients display actual server state.

## Client experience

- **Web**: `/yolo` slash command (status / on / off) with a browser
  confirmation dialog before enabling; a persistent, visible "⚡ YOLO ON"
  indicator while enabled (click = `/yolo off`); synchronized from server
  state on session switches and reconnects.
- **CLI/TUI**: the same Gateway API and decision semantics; a discoverable
  status/enable/disable flow; a persistent warning/status indicator while ON
  (the external TUI renders the session state — no local authority flags).
- **Future Telegram**: the same transport-independent API and session
  semantics; no client is an approval authority.

## Interaction with the risk classifier and saved rules

The classifier's three categories are preserved deterministically with a
versioned capability fingerprint; classification runs even when YOLO is on.
Saved exact-operation scoped rules keep their narrow semantics (and remain
the mechanism when YOLO is OFF); a YOLO operation never creates a permanent
rule and never broadens session scopes. A policy or classifier update cannot
silently expand an older permission (fingerprint fail-closed).

## Test evidence (increment 4)

`tests/test_yolo_mode.py` — 13 deterministic tests: default OFF; confirmed
enable / refusal without confirmation; disable always allowed; malformed
bodies fail closed; stale revision 409; cross-owner refused; state persisted
across reconnect and restart; unrelated sessions stay OFF; the risk prompt
suppressed with the audit record; DENIED/delegated boundaries intact; the
external-context gate suppressed for understood tools while unknown tools
stay refused; saved rules unchanged with YOLO off; read-before-edit still
enforced downstream; the loop threading from the admitted session only.

## Remaining M5.3 work

Chat/Agent UI unification and the hosted exit-gate verification (risky-command
prompts across all surfaces, the four decisions end-to-end, YOLO/deny/
owner-boundary negatives).
