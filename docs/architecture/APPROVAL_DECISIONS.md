# M5.3 — Approval Decisions and Scoped Rules (Increment 2)

The Gateway approval system answers every gated action with four consistent,
Hermes-style decisions through ONE shared mechanism (`src/tool_approvals.py`,
`src/tool_approval_scopes.py`, `src/tool_approval_rules.py`,
`src/gateway_approval_decisions.py`). Deny is the default when no valid
decision arrives: a timeout, a malformed decision, a disconnected approval
channel, or an unauthorized responder all fail closed to denial.

## The four decisions

| Decision | Wire value | Immediate sealed action | Repeats |
| --- | --- | --- | --- |
| Allow once | `allow_once` | executes exactly once | never — the gate re-arms for the next round |
| Allow for this task | `approve_task` (legacy-compatible) | executes | every otherwise-gated action needed to finish this request |
| Allow for this chat session | `approve` | executes | later gate prompts in this chat session |
| Always allow this specific rule | `always_allow` | executes | future matching operations, via a saved narrow rule |
| Deny | `deny` | never executes | never |

Every decision consumes the pending approval exactly once (atomic, one-use).
A replayed decision or operation cannot execute again; a changed command,
changed arguments, a different tool, or a different directory requires fresh
authorization. The pending approval expires per the existing contract
(default 10 minutes); expired approvals cannot be revived.

## Exact-operation authority is unchanged

A scoped rule — session or persistent — suppresses ONLY the automatic
approval PROMPT for the exact reviewed operation. Every approved execution
still passes all remaining gates in order:

1. Gateway authentication and owner/session access checks.
2. Current tool admission and policy (specific denials stay authoritative —
   a rule never overrides a policy denial).
3. Request-authority grants (a saved approval never expands authority).
4. Resource and CWD binding (the rule binds the reviewed directory; a session
   whose CWD changed must request fresh authorization).
5. The operation's capability fingerprint (effects + result integrity): a
   classifier or policy change that alters the classified shape makes saved
   rules inert (fail-closed).
6. Sealed exact-operation validation, replay/cancellation/expiry checks and
   audited dispatch.

The model cannot mint, widen, or invoke a rule: rules are created only by an
explicit user decision on a genuine pending approval, are never reconstructed
from client-visible cards, and the tool identity matches through the existing
normalization boundary (handler id or canonical name — the same tool; MCP
names are never translated).

## Scoped-rule representation

Each saved rule records: rule id, owner, session id (`""` = persistent across
the owner's future sessions), internal handler id + canonical model-visible
name, the EXACT reviewed operation content (bounded), the effective workspace
(realpath), the operation digest over the whole binding payload, the
capability fingerprint, provenance (origin approval id), created/updated
timestamps, optional expiration, a store version, and revocation state.

There are NO wildcard or executable-prefix rules: compound commands, command
substitution, redirections, scripts and delegated subprocesses are separate
operations that never inherit one component's approval. Narrower pattern
shapes (Claude Code-style `Tool(specifier)` rules) belong to the deterministic
risk-classifier increment and are intentionally absent here.

## Persistence, expiry, revocation

- Persistent rules are stored in the trusted application data store
  (`DATA_DIR/approval_rules.json`, versioned, atomic replace, bounded per
  owner) and survive server restarts.
- Session-scoped grants live in the session machinery (existing chat-session
  grant markers, restart-safe through the signed transcript grant) and expire
  with the session; in-memory session rules expire with the pending-approval
  TTL.
- Rules are inspectable (`GET /api/v1/approval-rules`) and revocable at any
  time (`DELETE /api/v1/approval-rules/{rule_id}`, owner-checked); revocation
  applies to subsequent operations immediately.
- A malformed store fails closed: unreadable JSON, unknown versions, or
  malformed entries load no rules — nothing is silently reinterpreted as a
  permission. Old approval records are never promoted to permanent
  permissions.

## Client integration

- The Web approval card renders the server-provided options (labels, scope
  descriptions, values) — the four decisions plus the legacy task scope for
  in-flight cards. The always-allow description states the permanent,
  narrow, revocable effect explicitly.
- CLI/TUI submit the same wire values through the same routes; no client is
  an approval authority.
- A future Telegram adapter displays the same operation card and submits the
  same four decisions; the approval contract is transport-independent.
- Durable turn records keep settling sealed decisions against the original
  owner, session, turn, tool, operation digest and resource identity.

## Test evidence (increment 2)

`tests/test_approval_decisions_rules.py` — 24 deterministic tests: scope
mapping for all five wire values; allow-once never widening; deny consuming
the prompt without granting and refusing revival; the card offering the four
decisions with explicit scope text; persistent rules matching the exact
operation across sessions and refusing changed content/CWD/tool/owner,
compound commands, expired/revoked/malformed stores, and capability
fingerprint changes; restart survival; deduplication; per-owner bounds; and
the Gateway decision boundary (allow-once consumes without saving,
always-allow persists a narrow rule, unknown decisions fail closed without
consuming the prompt, deny grants nothing).

## Remaining M5.3 work

The deterministic dangerous-command classifier (rule triggers beyond the
untrusted-context gate), opt-in YOLO mode, the Chat/Agent UI unification, and
hosted exit-gate verification.
