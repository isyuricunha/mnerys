# Native Notes Decision Fence — Deployment and Recovery

**Status:** Experimental M2.1. This document is a migration plan, not permission to deploy or modify a live database.

The [notes decision fence](../../src/gateway_notes_decision_fence.py) adds a metadata-only transactional ledger for the guarded notes_v1 HTTP path. It stores an owner, session identifier, opaque approval identifier, original committed user receipt, original action digest, expiry, and monotonic state. It does **not** store tool commands, note content, model output, authentication tokens, credentials, or a reconstructable tool grant.

## Safety contract

The original, owned ToolApprovalStore and committed user receipt must be verified before registering a pending review, which happens before an actionable browser card. The original human authentication, account policy and exact-action validation remain mandatory. Following all preflight checks and immediately **before** the original store consumes an approval, a conditional SQL UPDATE moves the pending row to reserved or denied. A reservation cannot be claimed by a second worker. After the original assistant writer confirms its receipt, the metadata moves from reserved to completed.

An interrupted process may leave a reserved row with no saved assistant receipt. This is an **uncertain effect**, not permission to retry. The code must not attempt automatic replay or infer the action never happened. The following states move one way only: pending to reserved to completed, or pending to denied/superseded. Expiry prevents late claims. SQL metadata itself never grants a tool.

**Read-only restart status:** if the original process-local run is missing, the owner-admitted interactive Web session can consult the ledger by its **latest committed user-message receipt**. The status endpoint returns only a non-actionable classification: `interrupted` (pending), `expired`, or `indeterminate` (reserved, possible effect). It never returns an approval ID, digest, tool text, cursor or permission. Reserved effects take priority over another pending review of the same original message; later assistant/user history prevents stale warnings. The browser displays this as an ordinary themed status notice with **no approval buttons**, never as a restored tool card. A noninteractive/delegated credential cannot query the status.

The separate [restricted event journal](notes-event-journal.md) adds
bounded, read-only server milestone replay and explicit manual retention.
It does not replace this one-use fence, recreate a signed approval, or
persist raw SSE events. Both independently reviewed migrations must be
installed before the `notes_v1` HTTP profile can be enabled.

**Important limitation:** the sealed PendingToolApproval and typed event buffer still live only in process memory. Restart or worker replacement cannot reconstruct the original action safely; this table alone is **not** durable approval-card or tool execution recovery. A lost run's read-only status does not prove that an effect did or did not happen.

## Migration procedure — operator-approved only

The canonical schema is in [the decision-fence module](../../src/gateway_notes_decision_fence.py). It is intentionally excluded from the automatic Base.metadata startup. The [versioned installer](../../scripts/migrations/notes_decision_fence_v1.py) must be run separately; it creates a new table and partial unique index, without altering existing tables or deleting data.

Before installing on any real server, verify its exact database identity, create a consistent backup outside Git and public work artifacts, **test restoration in an isolated database**, inspect the migration/schema diff, and obtain explicit approval. For PostgreSQL 17 use an isolated backup/restore check; for SQLite use a consistent SQLite backup with WAL/sidecar handling. Keep credentials and private database contents out of logs and arguments.

Provide DATABASE_URL through the secured runtime environment and run a **read-only check**:

    python scripts/migrations/notes_decision_fence_v1.py --check

A missing table reports missing and exits nonzero; a partial schema fails closed. Only after the approved backup and restore check, install the independent table using both explicit safeguards:

    MNERYS_NOTES_FENCE_MIGRATION_APPROVED=1 \
      python scripts/migrations/notes_decision_fence_v1.py --apply --verified-backup

The installer does not print database URLs, does not automatically repair existing schemas, and refuses absent backup approval or missing explicit DATABASE_URL. Rerunning the installer against a compatible table is read-only. No service flag is changed by the migration.

## Rollback, retention and activation

**Code rollback is not database rollback.** To revert application behavior, disable MNERYS_M21_WEB_NATIVE_NOTES_ROUTING and preserve rows for review. Never drop the ledger automatically: uncertain reservations may correspond to real effects. Any destructive rollback requires a separate authorized, backed-up data migration.

Owner-scoped retention, export and deletion policies are still unimplemented. SQLite sidecars and PostgreSQL backups deserve the same protections as chat data. Do not enable this profile for real users until reviewed pruning and recovery behavior, full browser/provider acceptance, and multi-worker fencing are completed.

SQLite memory/file and disposable PostgreSQL 17 acceptance covers concurrent claims, expiry, denial, stale origins, duplicate actions, schema checks and process-like reopen. The migration and HTTP tests use only isolated databases, never production services:

- [Fence tests](../../tests/test_mnerys_gateway_notes_decision_fence.py)
- [Migration tests](../../tests/test_mnerys_gateway_notes_fence_migration.py)
- [Guarded HTTP tests](../../tests/test_mnerys_gateway_notes_decision_http.py)
- [Read-only restarted status tests](../../tests/test_mnerys_gateway_notes_read_only_recovery.py)
- [Browser tests](../../tests/e2e/gateway-notes/)

Implementation references: [SQLAlchemy transactions](https://docs.sqlalchemy.org/en/20/core/connections.html), [metadata creation](https://docs.sqlalchemy.org/en/20/core/metadata.html), and [PostgreSQL partial indexes](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).

**Open:** restart-safe sealed approval reconstruction, durable SSE event replay, multi-worker session fencing, ambiguous-effect reconciliation, data retention, full CI and real-account/provider acceptance. The read-only status is implemented and tested; it is not a replay journal.
