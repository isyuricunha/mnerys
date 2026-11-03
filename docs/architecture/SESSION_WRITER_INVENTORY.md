# M1 — Session write-path inventory and migration gates

**Status:** source-audited migration inventory, not a claim that writers are unified. See the [roadmap](../ROADMAP.md), [Gateway contract](GATEWAY_AGENT_CONTRACT.md) and [revision migration plan](SESSION_REVISION_MIGRATION.md).

## Why this inventory matters

The legacy `sessions` table has no revision/incarnation identity. Updating metadata through a new CAS repository while leaving another writer unguarded would create a false guarantee. The new [fenced writer prototype](../../src/session_write_fencing.py) is therefore **not wired to any existing route, task, or tool**.

The following paths were checked against the current source; links point to modules, not immutable line numbers. This list covers **identified direct and indirect session write families**, not a guarantee that all dynamic integrations have been audited.

| Path | Existing writes | Requirement before activation |
| --- | --- | --- |
| [SessionManager](../../core/session_manager.py) | Create, delete, `_persist_message`, truncate, replace history, rename, archive, importance, cleanup and lazy/in-memory cache synchronization | Make session generation and revision mandatory on all mutations; include message counters/history in guarded transactions; synchronize caches **after commit** |
| [Chat routes](../../routes/chat_routes.py) and [chat helpers](../../routes/chat_helpers.py) | User/assistant messages, stream completion, stopped/error partial responses, endpoint credential metadata and automatic naming | Carry a server-issued fence across each turn; guard every terminal and cancellation persistence branch; preserve upload sanitization and exact approval semantics |
| [Session routes](../../routes/session_routes.py) | Rename, edit/delete/restore/import/history, bulk delete, session flags and message metadata | Use a single owner-scoped repository for every mutation; specify conflict responses, transcript revisions and delete/recreate handling |
| [Background jobs](../../src/background_tool_jobs.py) and [monitor](../../src/bg_monitor.py) | Job origin/delivery markers and chat-message delivery, including direct `ChatMessage` inserts | Bind immutable session/turn incarnation to job origin; write delivery marker and message within a fenced transaction; preserve once-only/idempotent delivery |
| [Agent session tools](../../src/agent_tools/session_tools.py) | Agent-triggered new sessions, rename, archive, delete, truncate, copy and cross-session messages | Enforce the same admission/authorization policy as Web, and recheck owner/generation/revision inside each transaction |
| [Webhooks](../../routes/webhook/webhook_routes.py) and [research routes](../../routes/research/research_routes.py) | Asynchronous/session-backed message insertion and research-session creation | Persist under the original approved owner and session incarnation, not ambient scheduler/tool privileges |
| [Admin wipe](../../routes/admin_wipe/admin_wipe_routes.py), [auth](../../routes/auth_routes.py) and [housekeeping](../../core/session_manager.py) | Bulk delete, account-owner rename and timed cleanup | Plan explicit privileged bulk-operation transactions, ownership transfers and generation invalidation; never silently bypass the write protocol |
| [Cookbook routes](../../routes/cookbook_routes.py) and other legacy utility integrations | Session-related metadata and delete paths alongside unrelated database operations | Audit query targets and direct SQL before rollout; do not assume all `db.commit()` calls write sessions |

The codebase also has other data stores (documents, mail, calendar, memory, tasks) with their own transactions. They must retain independent persistence and ACLs; merely sharing the SQLAlchemy engine does not make those records part of the session revision protocol.

## Safe prototype now available

The new `SqlFencedSessionWriter` provides a **test-only** implementation of the future mutation boundary:

1. `read_fence(admission)` returns the currently owner-visible `(session_id, incarnation_id, revision)` snapshot.
2. `patch_metadata` applies an explicit allowlisted metadata change only when session ID, owner, incarnation and expected revision all match in one SQL `UPDATE`.
3. `append_prepared_message` runs the same atomic guard, increments the persisted message count and inserts a message in the **same transaction**, with rollback on failure.
4. Old revisions, deleted/recreated sessions with the same ID, changed owners, missing/invalid schemas and invalid admissions are rejected. It does not automatically retry a conflicting write.

A prepared message is **not** a client-supplied message or a completed sanitization pipeline. Before activation, the adapter must call the inherited `persistable_message_content`, upload reservation/validation and metadata handling, enforce message-id/idempotency policy, and update in-memory state only after a confirmed commit. None of those legacy production writer call sites were replaced.

**Compatibility boundary:** `src/session_revision.py` remains the earlier **revision-only experimental metadata CAS**. It lacks session-incarnation fencing and must not be activated on real sessions. The newer [incarnation-fenced prototype](../../src/session_write_fencing.py) is the intended direction for further M1 exploration, but is also **disabled**.

## Required migration and verification sequence

1. Build an exact dependency map for **every** direct write and background callback. Review authorization changes, session delete/create reuse, old imports and non-HTTP tools.
2. Confirm successful backup **and isolated restore** of the actual database version before any real migration. PostgreSQL restore and SQLite backup require separate tested paths.
3. Design dialect-specific migrations for `revision` and a **unique, immutable, non-null session incarnation ID**. Backfill all existing rows without losing messages, owners or session configuration. New sessions must receive a new generation ID even when an ID is reused.
4. Replace all writers as one coordinated compatibility migration. If legacy writers can remain live during upgrade, enforce revision/generation at the database boundary rather than trusting a Python convention alone.
5. Introduce explicit session-write transaction and idempotency contracts, including partial responses, job delivery and cancellation. Define whether same-session concurrent turns are rejected or queued.
6. Prove SQLite and PostgreSQL 17 parity, multi-process concurrency, restart recovery, backup/restore and failure rollback in disposable environments before approving production cutover.
7. Obtain separate approval for any live migration, then follow a monitored staged rollout with verified recovery; a code checkout alone cannot undo database changes.

## Current test evidence

- [SQLite/PG17 fenced transaction tests](../../tests/test_mnerys_session_write_fencing.py): identical contract tests against file-backed temporary SQLite and a disposable PostgreSQL 17 container.
- [PG17 synthetic legacy-schema migration preview](../../tests/test_mnerys_session_migration_pg17_preview.py): additive revision/incarnation columns, deterministic per-row backfill, non-null/unique constraints, retained messages, stale-CAS rejection and transactional DDL rollback.
- [Legacy revision-only SQLite tests](../../tests/test_mnerys_session_revision.py): retained as characterization, **not** a safe production migration.

PostgreSQL tests require `MNERYS_M1_PG_TEST_DSN` and refuse any server that is not **PostgreSQL 17**, database `mnerys_m1_isolated`, and user `mnerys_m1_test`. The local developer harness creates its own temporary Docker container, publishes PostgreSQL on a random **loopback-only** port, uses transient storage, and removes the container afterward. The harness is kept in a maintainer-only working directory outside the repository; it is not an installed application component or CI guarantee.

The [PostgreSQL 17 Read Committed documentation](https://www.postgresql.org/docs/17/transaction-iso.html) confirms that concurrent `UPDATE`s wait and recheck the predicate against the updated row. The [SQLAlchemy versioning guide](https://docs.sqlalchemy.org/en/20/orm/versioning.html) notes that mapper-level versioning does not protect bulk `Query.update()`; explicit CAS predicates and affected-row checks are used in these test-only adapters.

**Remaining verification gaps:** real inherited schema migration and restoration, SQLite table-rebuild strategy for non-null generation columns, full production workload simulation, PostgreSQL nondefault isolation levels, durable run/event records and zero-overlap deployment sequencing.
