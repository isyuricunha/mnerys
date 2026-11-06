# M2.3 durable turns, events and approvals

**Status: IMPLEMENTED (store + typed-adapter integration + sealed approval
recording). The tables are additive and explicit-migration-gated; nothing is
auto-installed and no live database was migrated.**

## Contract

- **One durable, owner-scoped turn record** (`gateway_turns`) per admitted
  turn: exact turn id, session id, session incarnation, optional idempotency
  key, server policy version, cancellation token, and one deterministic
  terminal state (`completed`, `failed`, `cancelled` or
  `awaiting_approval`). Finalization is exactly once: a second finalize or a
  late event after the terminal is refused.
- **Ordered, versioned, sanitized events** (`gateway_turn_events`) with an
  immutable per-turn `sequence` (unique `(turn_id, sequence)`): a failed or
  filtered event leaves an explicit gap that readers see, never a rewritten
  history. Payloads are bounded JSON written only by the sanitized typed
  adapter (`src/gateway_turn_records.py` enforces the object shape and the
  64 KiB bound). Raw provider data, credentials, tool arguments/results and
  private identity are forbidden in these payloads.
- **Sealed approvals** (`gateway_approvals`) bound at creation to owner,
  session, incarnation, origin turn, tool name, operation digest, resource
  identity and effective authority, with an expiry. Consumption is one-use:
  the durable decision is recorded only after the in-memory sealed store
  consumed the card, and replay is refused before any durable write. A
  process restart can never reconstruct execution authority from UI cards,
  journal metadata or the records themselves.

## Integration points

- `GatewayTurns.open_composed_text_events(..., records=SqlTurnRecords,
  turn_id=...)` opens the durable turn (idempotency-key safe, session
  incarnation required) and persists every typed event as it is emitted;
  the terminal event is recorded before the record is finalized, and
  cancellation, provider failure, incomplete stream and stream exception
  all finalize `failed`/`cancelled`.
- `GatewayTurns.decide_tool_approval(..., records=..., session=...)` seals
  and consumes the approval record durably after the in-memory consumption.
- `Expire`/prune: `expire_pending_approvals` expires pending records past
  their deadline; `prune_terminal_turns` enforces bounded retention with an
  explicit cut-off and batch. Retention automation runs from the Gateway
  maintenance path when the records are enabled.

## Migration

`scripts/migrations/gateway_turn_records_v1.py` installs the three additive
tables, read-only by default and refused without a verified backup/restore
and operator approval. It never touches other tables. The PostgreSQL
startup schema guard in `core.database` fails closed until the migration is
applied, exactly like the M2.2 fencing columns.

## Evidence

- Repository acceptance: `tests/test_mnerys_turn_records.py` (lifecycle,
  exactly-once terminal, immutable sequences with visible gaps, idempotency
  key reuse, owner isolation, approval sealing/expiry, retention pruning,
  invalid input rejection) on disposable SQLite and PostgreSQL 17.
- Adapter integration: `tests/test_mnerys_turn_records_integration.py` (a
  real composed turn persists its sanitized typed events and finalizes;
  cancellation finalizes `cancelled`; a fresh interpreter reads the
  finalized turn; an approved card is sealed once with digest, turn, tool
  and authority binding and replay is refused).
- Backup/restore drill: the disposable PostgreSQL 17 drill passed with the
  full fencing and records suites (97 tests), then a seeded fenced schema
  was dumped, dropped, restored and validated (columns and backfill intact).
- No live database was modified; the tables remain uninstalled until the
  explicit, approved migration runs.