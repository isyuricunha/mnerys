# Restricted Notes Event Journal — Schema, Replay and Retention

**Status:** M2.1 experimental, behind the disabled-by-default
`MNERYS_M21_WEB_NATIVE_NOTES_ROUTING` flag. This is not authorization to
migrate, activate, deploy or edit a real database.

The journal persists **only server-written lifecycle metadata**, separate
from the [durable one-use decision fence](notes-decision-fence.md).
It does not store notes, prompts, tool arguments/results, rendered assistant
text, bearer credentials, signed approval payloads or SSE frames.

## Accepted events and effects

- `review_registered`: the original sealed review and committed user receipt
  were verified, and its one-use SQL fence was created **before** the
  actionable browser card could be displayed.
- `decision_reserved`: the original human decision passed preflight and the
  SQL fence was atomically reserved **before** the Core could consume the
  approval or invoke a tool. This does **not** prove the tool succeeded.
- `decision_denied`: a denial was recorded as a terminal decision.
- `assistant_committed`: the original writer confirmed the assistant
  message receipt after a reserved effect. The journal never changes the
  confirmed message, undoes a tool effect or schedules a retry.

Each stored row has an internal monotonic database sequence and a
**random 32-character opaque pagination token**, plus owner, session ID,
original user receipt, internal opaque approval ID, type and timestamp.
There are **no arbitrary JSON payloads or executable inputs**.
`(approval_id, kind)` is unique; duplicates cannot insert a second event.
SQLite uses `AUTOINCREMENT` so internal positions do not recycle after
pruning; PostgreSQL uses its database sequence. Neither the global
sequence nor the internal approval ID appears in client responses.
An opaque cursor must match an existing event **within the authenticated
owner/session/original receipt scope**; foreign or pruned cursors fail closed.
The original SQL decision fence, not the journal, controls at-most-once
tool dispatch.

The optional journal write is intentionally on a separate transaction from
the inherited message writer and the existing decision fence. A journal
failure **before** an approval is displayed prevents that card from being
shown. If writing the reservation marker fails **after** a successful SQL
reservation, the approval remains reserved/indeterminate and cannot be
executed again. A missing `assistant_committed` marker after a confirmed
writer receipt does not revoke that receipt, retry the model or re-run the
tool; the fence remains authoritative. This is not atomic whole-turn
persistence.

## Restricted read-only replay

`GET /api/chat/notes_journal/{session_id}?limit=50`

Subsequent pages use `after_cursor=<opaque-token-from-previous-page>`.

This optional route requires the existing interactive owner admission,
the server-side notes feature switch and a previously selected session.
Delegated credentials cannot read it. Only events for the **current
committed user message** (or its immediately following confirmed assistant
message) are projected. Cross-owner/session/receipt rows are excluded;
newer user turns hide earlier timelines. The response contains only
`gateway_profile`, an `events` array with `cursor`, `kind`, and
`recorded_at`, and the resulting `next_cursor`.
`limit` is restricted to 1–100; cursors are 32-character opaque tokens
issued by the original journal and checked inside the same owner scope.
Duplicate, malformed, foreign, stale or unbounded query arguments fail closed.

These event IDs are **journal pagination cursors**, not SSE run cursors,
signed review IDs or tool capabilities. The endpoint never recreates
`ask_user`, accepts an approval, shows the original model content or
starts an agent. The original post-restart `review_unavailable` notice
remains non-actionable and independent of journal playback. The Web session now fetches the first 50 scoped milestones only after a
`review_unavailable` result and shows at most the last four as fixed,
non-actionable labels under the existing warning. Unknown kinds, private
cursors, timestamps and other JSON fields are never rendered. Journal
HTTP/JSON failures leave the original decision-fence warning intact;
changing sessions while the request is pending prevents late rendering.
This is not a full timeline or an executable workflow.

## Schema installation — separate operator approval required

The schema is defined by
[`src/gateway_notes_event_journal.py`](../../src/gateway_notes_event_journal.py)
and installed with
[`scripts/migrations/notes_event_journal_v1.py`](../../scripts/migrations/notes_event_journal_v1.py).
It is **not** included in application startup `Base.metadata`.
The journal migration requires the decision-fence table to be installed
first. The Web route checks both schemas *before* any new notes turn.

Follow the verified backup/restore and database identity procedure in the
[decision fence deployment plan](notes-decision-fence.md). Never put a
credentialed connection URI into a command, Git, output or public log.
Set `DATABASE_URL` through an already secured operator environment.

Read-only schema check:

```bash
python scripts/migrations/notes_event_journal_v1.py --check
```

Only after an independently verified backup, isolated restore, reviewed
migration plan and explicit operator authorization:

```bash
MNERYS_NOTES_JOURNAL_MIGRATION_APPROVED=1 \
  python scripts/migrations/notes_event_journal_v1.py --apply --verified-backup
```

The script creates only the new independent table and indexes; it does not
alter, drop or repair existing tables. An incomplete or incompatible table
is a fail-closed error requiring manual reconciliation.

## Retention, rollback and restart

The journal has **no automatic retention job**. The reviewed internal
`prune_terminal(older_than_seconds, max_rows)` method refuses retention
shorter than 30 days, processes at most 1,000 events per call and deletes
only events whose independently stored decision fence is
`completed`, `denied` or `superseded`. It never prunes pending or
reserved/uncertain actions. **This method is not an approval to schedule a
job on production data.** The decision fence is never deleted by pruning.

For rollback, disable `notes_v1` before code changes. Preserve the
journal and decision fence for review: rollback of code does not restore
database state, and uncertain reservations may correspond to real effects.
Any additional deletion, retention automation or data migration requires a
separate audited plan and tested backup.

The journal survives reopening a database, but **not** every aspect of the
Gateway lifecycle. The signed PendingToolApproval and original SSE buffer
remain process-local. Replayed milestones can never restore a signed review
card, dispatch a tool or automatically reconcile an uncertain effect.
Durable authenticated stream delivery, full per-run concurrency fencing,
multi-worker session leadership, data retention authorization, real-provider
acceptance and the full four-shard CI gate remain open.

## Evidence

- [Journal contracts and two-worker tests](../../tests/test_mnerys_gateway_notes_event_journal.py)
- [Explicit migration tests](../../tests/test_mnerys_gateway_notes_event_journal_migration.py) — including fresh-engine readback on isolated SQLite and disposable PostgreSQL 17
- [Authenticated read route and failure tests](../../tests/test_mnerys_gateway_notes_event_journal_http.py)
- [Independent process restart and isolated backend refusal tests](../../tests/test_mnerys_gateway_notes_restart_process.py) — no provider, signed-approval recovery or tool replay
- [Synthetic Chromium HTTP tests](../../tests/e2e/gateway-notes/integrated-browser.spec.js)

Implementation references:
[SQLAlchemy Engine.begin transactions](https://docs.sqlalchemy.org/en/20/core/connections.html),
[SQLAlchemy metadata creation](https://docs.sqlalchemy.org/en/20/core/metadata.html),
[PostgreSQL index and uniqueness behavior](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).
