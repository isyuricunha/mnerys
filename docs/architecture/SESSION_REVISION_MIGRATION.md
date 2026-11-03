# M1 session revision and concurrent-write migration plan

**Status:** revision-only SQLite prototype, incarnation-fenced SQLite/PostgreSQL 17 writer tests, and a synthetic PostgreSQL 17 legacy-schema migration preview are verified. **NOT activated** on any production database or existing Web/API route. No application schema migration has been applied or generated.

This document accompanies the [Mnerys roadmap](../ROADMAP.md), [Gateway contract](GATEWAY_AGENT_CONTRACT.md), [writer inventory](SESSION_WRITER_INVENTORY.md) and [M1 regression baseline](M1_CONTRACT_TEST_BASELINE.md).

## Current findings

The inherited `core.database.Session` model does not contain a non-null integer revision, and `core.session_manager.SessionManager` still writes session metadata and messages directly. In particular:

- `_persist_message` writes a message and counters in one DB transaction; it does not check run identity or a session revision.
- `replace_session_history`/truncate, renames, archive, importance changes and cleanup use independent write paths.
- `routes/chat_routes.py` still calls assistant persistence from multiple error, cancellation and terminal branches.
- `src.agent_runs` serializes *in-process* replacement by waiting on the previous drain task, but its replay buffers and cancellation IDs do not survive process restart.
- Session deletion and ghost-session fallback exist; these are not transactional run-fencing mechanisms.

**Therefore adding a revision column alone is insufficient:** any remaining unversioned write path could bypass the check, and concurrent writers could still lose updates.

## Implemented opt-in prototype

`src/session_revision.py` provides:

- `SessionMetadataChanges`: frozen allowlisted patch of `name`, `archived` and `is_important`; no arbitrary SQL field or user-provided owner change.
- `SqlSessionRevisionWriter.current_revision(admission)`: only reads a revision for a previously admitted, currently owner-matching session.
- `SqlSessionRevisionWriter.patch(admission, expected_revision, changes)`: a single SQL `UPDATE` guarded by **session ID, current owner and expected revision**. Updates the metadata and increments revision in the same database transaction.
- `SessionRevisionConflict`: stale, deleted and foreign rows are indistinguishable. A failed guard never applies the metadata patch. A DB/commit error rolls back.
- `SessionRevisionUnavailable`: fails closed when the supplied model lacks a revision field, or a stored revision is null/invalid.

This **revision-only** primitive cannot presently be invoked with the inherited `DbSession` model. It does not guard deletion/recreation of an identical session ID and **must not be enabled in production**. It remains a characterization experiment, not a durable turn fence or an end-user endpoint. No additional runtime dependency was introduced.

The [SQLAlchemy version counter guide](https://docs.sqlalchemy.org/en/20/orm/versioning.html) notes that mapper-level `version_id_col` checks apply to ORM flush operations, not bulk `Query.update()`. This prototype intentionally uses an explicit revision predicate and checks the affected-row count, consistent with the [SQL UPDATE and rowcount documentation](https://docs.sqlalchemy.org/en/20/tutorial/data_update.html).

## Additional incarnation-fenced writer prototype

The new [`src/session_write_fencing.py`](../../src/session_write_fencing.py) extends the **test-only** contract with an immutable `SessionWriteFence(session_id, incarnation_id, revision)`. The future schema must generate a **unique new incarnation** for every lifetime of a session ID. Both `patch_metadata` and `append_prepared_message` condition one atomic SQL UPDATE on the admitted owner, session ID, revision and incarnation. The message path inserts exactly one pre-sanitized message and increments the stored counter in the same transaction. Conflicts and insert failures roll back completely.

The writer is **not wired into the existing SessionManager, chat routes, jobs or tools**. It does not yet implement upload reservation, content sanitization, full message editing/truncation, cache synchronization, durable turn records or idempotent job delivery. Those operations must be migrated together, not bypassed.

## Isolated tests

`tests/test_mnerys_session_revision.py` creates a *separate* test table in a temporary SQLite database; it does not use `core.database.Session` or any production connection. It covers:

- Atomic changes and monotonic revision increments;
- Stale-writer rejection without overwriting a committed change;
- Owner change after admission, missing sessions, null-owner and delegated-token isolation;
- Two concurrent transactions using separate SQLite connections, with exactly one accepted;
- Invalid inputs and absent schema, rejecting before any production connection;
- Rollback when commit fails after the guarded UPDATE.

The original **revision-only** test suite remains SQLite-only. New [fenced write tests](../../tests/test_mnerys_session_write_fencing.py) passed against **both temporary SQLite and a disposable PostgreSQL 17** database, including a two-writer conflict, a deleted/recreated session ID with a different incarnation, owner changes, metadata/message atomicity and rollback on insert failure.

A separate [PostgreSQL 17 migration preview](../../tests/test_mnerys_session_migration_pg17_preview.py) tested additive revision/incarnation columns and per-row backfill against a **synthetic legacy table**. It verified preserved owners/messages, non-null and unique incarnation constraints, stale revision predicates and rollback of PostgreSQL transactional DDL when a duplicate incarnation violates uniqueness. Together, those isolated PostgreSQL/SQLite suites passed **23 tests**; the temporary Docker container was removed afterward.

This is **not a migration of the inherited full Session schema**. Actual PostgreSQL 17 backup/restore, SQLite table rebuild, nondefault PostgreSQL isolation levels, restarts, durable event storage and all-writer rollout remain unverified. For the confirmed write-family map and remaining checks, see the [writer inventory](SESSION_WRITER_INVENTORY.md).

## Steps required before enabling real session writes

1. **Complete and validate the [writer inventory](SESSION_WRITER_INVENTORY.md)** (Web, background jobs, cleanup, imports, tools, messages and session metadata). Identify any dynamically loaded integrations and prove that history, attachments and counters remain consistent.
2. **Prepare and validate recovery:** take a tested backup of the current primary DB; verify a full restore on an isolated instance. A code rollback is not a DB rollback.
3. **Plan the schema migration** for both SQLite and PostgreSQL 17: a non-null integer revision, a safe deterministic initialization/backfill, non-destructive migration and a distinct session incarnation/generation marker to fence delete/recreate. Evaluate whether turn state needs its own transactional table and sequence.
4. **Migrate all competing writers and readers together:** add owner-scoped reads, revision propagation, atomic mutation+message commits, stable rejection/error semantics, and per-turn fencing. Never mix a revisioned writer with an unguarded writer in production.
5. **Add database-independent and PostgreSQL-specific concurrency tests:** two writers, cancellation during persistence, deleted/recreated sessions, restart during a turn, long-running tool results and concurrent cleanup.
6. **Roll out behind a guarded, reversible activation plan** with a read-only compatibility probe, health/readiness checks, manual approval for the DB migration, and a tested restoration procedure.

**Stop condition:** do not turn on the revision writer while the live schema or any competing writer lacks version enforcement. Never silently fall back to an unversioned write after a revision conflict or schema failure.

## Remaining planned responsibilities

The proposed public Gateway still needs authenticated remote clients, durable turns/events/approvals, operation audit, session-scoped writer admission, full owner-scoped CRUD and consistent cross-process policy. CLI/TUI are not yet Gateway clients.
