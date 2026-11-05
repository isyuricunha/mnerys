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

## M2.1 compatibility: admitted assistant terminal saves

**Implemented only as an admission/ownership boundary around the same writer;
no new database protocol or schema is activated.**
The five completed/terminal-failure assistant response saves in the
[Web streaming route](../../routes/chat_routes.py) now enter the
[Gateway-admitted persistence adapter](../../src/gateway_assistant_persistence.py)
and then invoke the unchanged
[legacy response saver](../../routes/chat_helpers.py).
The issued admission, loaded session, context owner and submitted session ID
must agree before any call to the writer. Existing response normalization,
tool/thinking metadata, incognito handling, message IDs, last-access updates
and write failures preserve their inherited semantics. There is still no
transactional pair across user and assistant messages.

**Incremental follow-up:** both stopped/disconnected partial-response
inserts are now checked by the same admitted Gateway boundary before
invoking the unchanged legacy `Session.add_message` and
`SessionManager.save_sessions` path. Successful Chat/Agent stream completion
also blocks another save on repeated DONE or disconnect, and the Chat partial
writer can no longer mask cancellation if it fails. This only prevents
multiple save attempts **within the same stream**; it is not a durable
idempotency key or a transaction fence.

**Not migrated to the revision-fenced writer:** synchronous Chat response
writes, all completed/interrupted Web saves, message-count consistency during
concurrent writes, and the new opt-in direct text persistence path (which
still delegates to the inherited writers without a revision fence).
The [fenced-writer prototype](../../src/session_write_fencing.py) remains
inactive in production. The [M2.1 exit gate](M2_1_EXIT_GATE.md) requires a
server-owned complete-turn writer and cross-client parity before any cutover.
[New admission/compatibility tests](../../tests/test_mnerys_gateway_assistant_persistence.py)
verify five Web save call sites and fail-closed owner checks without opening a
real database. This is **not** evidence of durable turn acknowledgements,
idempotent retries, transaction fencing or a tested production migration.

## Incremental live compatibility extraction: message preparation

**Implemented and exercised in the inherited server writers.** The lightweight [`src/session_message_preparation.py`](../../src/session_message_preparation.py) centralizes conversion of message content and metadata to one immutable `PreparedSessionMessage`. It reuses the existing `persistable_message_content` transformer and exact `json.dumps` metadata format, including stripping inline media data URLs from the persisted **content** field. It intentionally performs **no database access, session authorization, upload reservation, approval decisions or commits**.

The following existing code paths now call the same preparation function without changing their storage transaction or ownership policy:

- [`SessionManager._persist_message`](../../core/session_manager.py): still checks for a persisted parent session; reserves uploads and assigns a timestamp before calling the shared preparation function; assigns `_db_id` to the in-memory message only after commit
- [`SessionManager.replace_messages`](../../core/session_manager.py): still reserves **all** referenced uploads before deleting any old message; prepares each new message without repeating reservations, then commits the history replacement before replacing the in-memory history
- [Background job delivery](../../src/background_tool_jobs.py): still rechecks owner, job state and foreground-busy status, preserves the deterministic delivery message ID, and commits message plus delivery marker together; internally generated reports use the same persisted-content preparation

The [incarnation-fenced prototype](../../src/session_write_fencing.py) now imports that **same** `PreparedSessionMessage` value type for future migration; it is **not activated** on existing sessions. The changed production code remains on the inherited unversioned writers. This extraction does **not** guarantee that every session write is guarded, does not sanitize arbitrary metadata values, and does not replace any upload lifecycle checks.

Parity is covered by [dedicated serialization tests](../../tests/test_mnerys_session_message_preparation.py), inherited upload reservation and history replacement tests, and background-delivery regression tests. **Next:** implement an authenticated, incarnation-scoped writer adapter for each caller, with verified migrations, transaction idempotency and cache reconciliation before production activation.

## Experimental fenced delivery and cache reconciliation

**Implemented only in isolated test adapters; not enabled in the inherited scheduler.** The [fenced session writer](../../src/session_write_fencing.py) now supports a background-job delivery operation that requires an admitted session and a job row bound to the original owner, session ID and **session incarnation**. A single transaction performs the revision-checked session update, increments the message count, changes the job status and stores its deterministic message ID and prepared content. If the transaction fails, **all changes roll back**. Successful delivery can be retried without duplicate insertion only if the original session lifetime, persisted message, job marker, role, content and serialized metadata still match; changed reports or mismatched generations fail closed.

The job's future `origin_incarnation_id` column and the session's revision/incarnation columns **do not exist in the inherited production models**. The current [`BackgroundToolJobs`](../../src/background_tool_jobs.py) still uses its original unversioned transaction and deterministic message IDs; it is not connected to this adapter. Two different jobs targeting the same revision must retry with an updated fence, never bypass the check. A completed job marker does not, by itself, confer permission to write.

The [cache reconciliation helper](../../src/session_cache_reconciliation.py) is likewise **test-only**. It can publish a confirmed new message to a fully hydrated cache only when the admitted principal, session ID, owner, incarnation and previous revision all match the committed receipt. It returns `False` without mutation for a partial/stale cache or idempotent replay, requiring an authoritative reload. Future callers must retain original in-memory multimodal objects separately from the sanitized persisted payload.

**One real legacy cache fix was made:** [`SessionManager.replace_messages`](../../core/session_manager.py) now assigns new `_db_id` values to caller-owned messages **only after successful commit**. Previously a failed replacement could leave unpublished IDs in memory. Inherited upload reservation still precedes any history deletion; the database rollback leaves both the old transcript and cached history intact. The legacy [`Session.add_message`](../../core/models.py) and [`SessionManager.add_message`](../../core/session_manager.py) still append before persistence, and their broader failure/recovery behavior must be migrated with the rest of the writers; this change does **not** claim to solve it.

[Dedicated isolated background delivery tests](../../tests/test_mnerys_session_background_delivery.py) and [history rollback regression](../../tests/test_replace_messages_upload_reservations.py) cover retry/idempotency, changed payloads, session delete/recreate, changed owner, two concurrent workers, job status atomicity, insert failures, unauthorized sessions and cache refusal cases. The background and existing fenced-write suites passed **54 tests** together on disposable SQLite/PostgreSQL 17, including synthetic PostgreSQL migration-preview coverage. No live schema, client API, run manager or scheduler was altered.

The backend design relies on [PostgreSQL 17 Read Committed concurrency semantics](https://www.postgresql.org/docs/17/transaction-iso.html) and [SQLAlchemy 2.0 transaction/rollback behavior](https://docs.sqlalchemy.org/en/20/orm/session_basics.html). PostgreSQL's row predicate recheck is not a substitute for fencing **every** writer; the production migration remains separately gated.

## Incremental live compatibility fixes: append acknowledgement and agent tools

**Implemented on existing code paths; no revision schema activated.**
The inherited [`SessionManager._persist_message`](../../core/session_manager.py) returns an explicit success/failure boolean to its in-memory append paths. [`Session.add_message`](../../core/models.py) delegates to that process-wide manager **only when this exact session object is registered in its cache**. Independently created sessions (including another instance with the same ID) stay in-memory rather than accidentally persisting through an unrelated global manager. [`Session.add_message`](../../core/models.py) and [`SessionManager.add_message`](../../core/session_manager.py) discard **only their own uncommitted appended message** on a failed or missing-parent insert, without replacing the history list or deleting later messages by value. Unmanaged/ephemeral sessions continue to append normally. The existing failure logging and request API remain unchanged. A subsequent successful write can continue using the recovered cache. The writer still uses the legacy unversioned DB transaction and is **not** the incarnation-fenced adapter.

The [`manage_session` agent tool](../../src/agent_tools/session_tools.py) now commits a rename only once through its existing owner-scoped query, rather than committing and calling the unscoped `SessionManager.update_session_name` a second time. Rename, archive, unarchive and important-flag actions update only a **matching-owner cached session after DB commit**; a failed DB write or a foreign-owner cache entry is left untouched. The tool's existing current-session, important-session and owner restrictions are unchanged. No permission is granted by the cache helper.

[Disposable SQLite append/rollback tests](../../tests/test_mnerys_session_append_cache_rollback.py) verify failed inserts, missing/deleted parents, cache list identity, multiple equal-content messages, successful writes and unmanaged sessions. [Agent session metadata tests](../../tests/test_mnerys_agent_session_rename.py) cover single SQL rename, owner isolation, post-commit cache synchronization and rollback behavior.

**Still missing:** global all-writer revision enforcement, DB counters under concurrent transactions, durable turn acknowledgements, CAS-aware tool transactions, full job restart fencing and tested production schema migrations. These focused compatibility fixes are not a claim that any of those guarantees already exist.

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

## M2.1 direct text interruption compatibility (opt-in)

The [trusted direct text persistence adapter](../../src/gateway_text_persistence.py)
reuses the same admitted assistant-save and interrupted-response boundaries as
Web. In its default configuration, the direct composer performs no conversation
writes. With server-owned persistence, shared context ports append the user
input and the original assistant saver appends the successful reply; the
`message.final` and `turn.completed` notifications are held back until that
writer returns successfully. A final writer error is reported as a generic
persistence failure, without retrying a potentially committed write.

A separately supplied `interrupted_writer` enables compatibility saves for
nonempty, filtered visible deltas after early close, cancellation, provider
failure or an incomplete stream. The original admitted stopped-response writer
receives `stopped=True` plus a bounded internal interruption reason. It is
attempted once per stream even if its effect is ambiguous. Ownership is
rechecked at the persistence boundary; a changed owner prevents a late save.
Nothing grants tools or resumes a sealed approval.

These are **two independent inherited message writes**, not a transaction or
fenced turn receipt. A failed assistant write may leave a user-only message;
a failed partial write may have committed before raising. Cross-process replay,
exact-once retries, and production SQLite/PostgreSQL validation remain open.
The earlier typed-event suite uses synthetic model events and in-memory
writers; a separate suite now exercises the inherited SQLAlchemy writers
against disposable backends.

## M2.1 tested receipt and cached-owner compatibility guard

[Disposable SQLite integration tests](../../tests/test_mnerys_gateway_text_sqlite_integration.py)
exercise the inherited SQLAlchemy `sessions` / `chat_messages` tables and
the real SessionManager plus original assistant/partial writers, not a
mock-only append. A failed INSERT previously returned `False` from
`_persist_message` and rolled back the in-memory append without raising.
The direct persisted-text profile now checks for a fresh message `_db_id`
in the same session transcript after user and assistant writes. A missing
receipt cannot produce a successful direct-turn completion. This check does
not re-query the database or make the two message writes atomic.

The inherited SessionManager also checks the cached session owner against
the SQL row owner before appending a new message. If an ownership transfer
has already committed, stale cached sessions cannot append under the new
owner; failed writes leave the in-memory transcript consistent. Existing
unit fixtures now explicitly include the parent row's owner. These
checks do not stop races after the owner read, deleted/recreated sessions
with the same ID, or unguarded writers elsewhere. An incarnation/revision
fence still requires the coordinated migration described in
[SESSION_REVISION_MIGRATION.md](SESSION_REVISION_MIGRATION.md).

The same seven direct-turn integration cases now pass independently against
temporary SQLite and a disposable PostgreSQL 17 Docker container, with five
additional tests rejecting unsafe PostgreSQL targets before connection. The
opt-in PostgreSQL fixture checks loopback, database name, role and server major
version before creating tables. Its dedicated CI role/database are isolated
from migration tests. These are **legacy unversioned writer tests**, separate
from the previously tested fenced-writer prototypes. No production connection,
migration, concurrency guarantee, or restore was performed.

## M2.1 active-document read boundary (disposable SQL acceptance)

[The trusted document context resolver](../../src/gateway_document_context.py)
performs *read-only* SQLAlchemy lookup of one selected document. The existing
`documents` table and `Document.version_count` remain unchanged. The
direct context profile requires exact document owner/session and current
session-row owner, active/not archived state, and an unchanged content digest. It does not perform the implicit
cross-chat document rebind supported by the older Web editor.

For a sealed approval, this preflight is checked twice around asynchronous
context preparation, before consuming the one-use card. The inherited
`BoundOwnedOperation.validate` and document tool's expected version/digest
checks remain responsible for the execution-time identity and conflict
decision. Neither the Gateway lookup nor the approval consumption is atomic
with a document write, so a concurrent change may still require the
dispatcher to reject the attempted effect.

[Disposable SQLite/PostgreSQL 17 tests](../../tests/test_mnerys_gateway_document_approvals.py)
exercise the existing SQL schema, owner isolation, version/content changes,
archive/deletion, and the real dispatcher with a mocked final effect.
No production document mutation, schema migration, or backup is part of this
slice. Cross-session Web editor parity and all-writer storage fencing remain
open.

## M2.1 filesystem approval compatibility

[Direct filesystem approval preflight](../../src/gateway_filesystem_context.py)
reuses existing sealed `BoundFilesystemOperation` identities and
`resolve_filesystem_operation` rather than adding a filesystem writer or
deriving a root from the model's path. This introduces no database schema
change, migration or replacement of the legacy filesystem tools.

The preflight and dispatcher are separate checks: their success does not
make filesystem effects transactional with chat messages, approvals or
durable turn events. The existing tool's own path confinement, resource
identity, admin policy and effect tracking continue to govern execution.
[Isolated acceptance tests](../../tests/test_mnerys_gateway_filesystem_approvals.py)
use temporary directories only and include denial of a public non-admin
write. Full Web/Gateway parity, race-safe filesystem effects and durable
whole-turn settlement remain open.

## M2.1 direct model fallback persistence boundary

The direct composer now optionally delegates ordered owner-scoped fallback
routes to the **original Agent Loop** via the existing Gateway foreground
policy and resolver. No new conversation writer, response retry writer,
database table or fallback-specific save path was introduced. The direct
tools-disabled text persistence profile still uses the sequential admitted
user and assistant writers; it has no atomic whole-turn transaction or
durable cross-process retry receipt. The actual answering provider's model
and endpoint attribution are not yet settled by a complete direct/Web
parity harness.

The default direct profile remains strict and requires no additional
provider. Opting in requires both a trusted nonempty model allowlist and
the admitted owner's foreground fallback preference. Synthetic no-network
acceptance lives in
[direct fallback tests](../../tests/test_mnerys_gateway_direct_fallback.py).

## M2.1 direct upload reader with no conversation writes

The direct text-upload profile reuses the current owner's
`UploadHandler.resolve_upload`, existing uploaded-file manifest
and inherited ChatHandler text extractor. Selection and actual bytes are
validated before and after asynchronous preprocessing; only small text
files can enter the non-persisting direct request context. No alternate
upload database, session message writer, PDF document creator or file
mutation tool was added. The profile explicitly forbids conversation
persistence and tool-enabled turns; the Agent Loop receives untrusted text
context, never a new tool-visible upload path.

The existing upload index may still update access/reservation bookkeeping
through its original owner-scoped code; this does not create a new
message-write or exactly-once guarantee. See
[direct attachment acceptance](../../tests/test_mnerys_gateway_direct_attachments.py).
General multimodal upload/storage parity remains open.

## M2.1 typed fallback route observation (no new writes)

The source-owned `CoreModelRouteEvent` reports a committed fallback choice
within the process-local typed turn stream. It originates inside the
original model candidate selector and includes no provider credentials,
URL, diagnostic, document content or persisted receipt.

The existing Web fallback SSE, session writers and message persistence are
unchanged. No new turn table, event log, retry writer, transactional save
or durable per-round answering-model attribution was introduced. Refer to
the [typed-event contract](M2_1_TYPED_TURN_EVENTS.md#native-source-owned-model-route-selection).

## M2.1 detached typed response writer ordering

The direct typed composer can now run detached through the *original*
per-session [Agent run manager](../../src/agent_runs.py). Its run buffer
holds the terminal typed acknowledgement until the existing direct
persistence wrapper returns. The wrapper still invokes the inherited
admitted assistant writer for completed text and the interrupted writer
once for a visible stopped partial; no new message writer is introduced.

The run registry is process-local, with finite eviction; its resume cursor
is not a database receipt or a durable event cursor. Stop before task
entry prevents any context or writer I/O. Replacement awaits the previous
run's cancellation/partial-save cleanup to avoid interleaved session writes.
Existing SSE delivery and native Web adapter behavior remain unchanged.

See [detached lifecycle tests](../../tests/test_mnerys_gateway_detached_typed_lifecycle.py).
No atomic user/assistant transaction, generation fence, cross-process
replay, real-model acceptance or production Web cutover was added.

## Plain-text browser compatibility uses original assistant save

The new strict-text SSE preview adds **no message writer**. The already
admitted `save_admitted_assistant_response` call remains the only assistant
writer for the opt-in persisted direct turn. Its existing fresh-session
`_db_id` confirmation may now be echoed as a server-filtered
`message.saved` typed event before `message.final`, but only when a trusted
adapter explicitly enables this presentation option. The default typed
stream and the Web route's own writer/receipt remain unchanged.

For a late replay, the same original run manager checks whether the
buffered run emitted that receipt before the exact resume cursor.
This is evidence of a previously emitted frame, not another database read.
The confirmation is still a process-local, post-commit in-memory receipt
check. It is not a transactional turn fence, an independent SQL read,
cross-process idempotency or a durability guarantee for detached replay.
Failure to confirm saves produces a generic error without either the
receipt event or a completed SSE frame. See
[plain-text Web SSE tests](../../tests/test_mnerys_gateway_web_compat_events.py).
