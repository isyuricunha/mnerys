# Duplicate transcript records: read-only inventory and repair proposal

Read-only diagnostic snapshot of the private preview's persistent
transcripts, produced during the M4 stabilization pass (2026-11-06). No
record was modified, merged, or deleted. Session identifiers are salted
labels (`S-xxxxxx`); no content, user text, endpoint, or address appears
here.

## Source of truth and method

- Primary database: the preview's PostgreSQL 17 instance, queried with
  SELECT-only statements.
- Transcript table `chat_messages` has no turn id column; association with
  durable turn records uses session + time windows, so classifications carry
  confidence levels instead of hard links.
- The double-write defect (v0.15.2 and earlier) is already fixed by v0.15.3
  (the v1 route's second persistence wrap was removed; regression suite:
  `tests/test_mnerys_gateway_single_persist.py`).

## Inventory

A) Identical-content assistant pairs (candidate duplicate writes)

| Session label | Groups | Band | First seen | Confidence of duplicate write |
|---|---|---|---|---|
| S-cd2ba3 | 8 | instant (<2s) | 2026-11-07 00:27..01:08 | High: the v0.15.2 route double-persisted each completed turn; fixed in v0.15.3 |
| S-ae0097 | 2 | short (2-60s) | 2026-11-04 17:24 | Medium: consistent with resume/partial-then-full writes from the Nov 4 session |
| S-42b194 | 1 | short (2-60s) | 2026-11-06 10:02 | Medium: consistent with a resume-era write during M3 acceptance |

B) Orphan assistant messages (assistant rows with no user row earlier in the
same session): **0**. No assistant row exists without a corresponding user
input in the same session.

C) Completed turn records without any user row near the turn window: **0**.

D) Stale durable turn records left `running` (no terminal event): **2**, both
from client disconnects around a service restart (the restart killed the
in-process runs; the records never reached a terminal). Fixed by the startup
reconciliation (`reap_stale_turn_records`), which marks restart-orphaned
records failed at boot.

E) Identical-content user pairs: 2 sessions, both explained by intentional
repeat test sends (two identical long prompts sent on purpose during
disconnect/resume validation). Not duplicate writes.

## Proposed non-destructive repair plan (NOT executed; requires Yuri's
separate approval)

Scope: only the 8 instant-band pairs in S-cd2ba3 plus, if confirmed, the 3
short-band pairs. The goal is one transcript row per completed turn.

1. Selection criteria (high confidence only):
   - Same session, same role (`assistant`), byte-identical `content`.
   - Rows within the same turn window: both timestamps inside
     `[turn.created_at, turn.terminal_at]` of one durable `gateway_turns`
     record with `status='completed'`.
   - Keep the earliest row (the one whose metadata matches the recorded
     metrics); hide the later ones with a metadata flag
     (`{"hidden": true, "hidden_reason": "duplicate-write-2026-11"}`) rather
     than deleting. Display/history readers already skip hidden rows; the
     model-context loader (SessionManager.get_context_messages) does not, so
     the repair must either extend that filter in the same change or accept
     that hidden duplicates still reach model context (they are exact
     copies, so the only cost is context size).
2. Backup: a fresh verified PostgreSQL 17 archive immediately before the
   repair (the established helper + `pg_restore --list` verification), plus
   an isolated restore drill of that archive.
3. Dry-run: the same selection query runs in a transaction with ROLLBACK,
   and the report lists exactly which row ids would be flagged.
4. Transactional safety: the repair runs in ONE transaction per session
   (row flag + session revision bump) so a failure leaves the transcript
   untouched; concurrent writers are fenced by the existing revision guard.
5. Rollback: clear the flag on the affected rows (the backup archive remains
   available for a full restore if ever needed).
6. Verification: per-session before/after counts, a re-run of the inventory
   (expect 0 instant-band pairs), and one live read of the affected
   conversation in the Web UI confirming single rendering.

For the short-band pairs (S-ae0097, S-42b194), require the same criteria
plus an explicit confirmation that the earlier row is a partial save of the
same run (metadata/size analysis). If not provable, leave them untouched:
they are indistinguishable from legitimate repeated answers.
