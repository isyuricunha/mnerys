"""Non-authorizing, metadata-only durable notes_v1 event journal.

Only trusted server lifecycle milestones are accepted. This is NOT a durable
copy of SSE, a sealed review, a tool invocation, or an effect ledger. A replay
can never reconstruct a browser approval card or tool action.

Schema installation and retention are explicit operator operations. Nothing
here creates tables during application startup or performs automatic pruning.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import time
from typing import Literal

from sqlalchemy import (
    Column, Float, Index, Integer, MetaData, String, Table,
    UniqueConstraint, and_, delete, insert, inspect, select,
)
from sqlalchemy.engine import Engine

from src.gateway_notes_decision_fence import notes_fence_metadata

JournalKind = Literal[
    "review_registered", "decision_reserved", "decision_denied",
    "assistant_committed",
]
_KINDS = frozenset({
    "review_registered", "decision_reserved", "decision_denied",
    "assistant_committed",
})
_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_RECEIPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_APPROVAL_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{32}")
_MIN_RETENTION_SECONDS = 30 * 24 * 60 * 60
_METADATA = MetaData()

_JOURNAL = Table(
    "gateway_notes_event_journal", _METADATA,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    # Random per-row read-only cursor. Never expose database-global event_id,
    # which would reveal the activity volume of other sessions.
    Column("cursor_token", String(32), unique=True, nullable=False),
    Column("owner", String(255), nullable=False),
    Column("session_id", String(256), nullable=False),
    Column("origin_user_receipt", String(128), nullable=False),
    # Internal correlation only. Never returned in a replay.
    Column("approval_id", String(43), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("approval_id", "kind", name="uq_notes_journal_approval_kind"),
    Index("ix_notes_journal_scope_cursor",
          "owner", "session_id", "origin_user_receipt", "event_id"),
    Index("ix_notes_journal_retention", "created_at", "event_id"),
    # SQLite otherwise may recycle its largest deleted rowid after manual
    # retention, breaking an authenticated client's durable replay cursor.
    # PostgreSQL's identity sequence already keeps moving forward.
    sqlite_autoincrement=True,
)


@dataclass(frozen=True, slots=True)
class NotesJournalEvent:
    """Safe client projection, excluding grants, IDs of approvals and content."""
    cursor: str
    kind: JournalKind
    recorded_at: float


def notes_event_journal_metadata() -> MetaData:
    return _METADATA


def _identity(owner: str, session_id: str, receipt: str) -> tuple[str, str, str]:
    if (
        type(owner) is not str or not owner.strip() or len(owner) > 255
        or any(ord(ch) < 32 for ch in owner)
        or type(session_id) is not str or _RE.fullmatch(session_id) is None
        or type(receipt) is not str or _RECEIPT_RE.fullmatch(receipt) is None
    ):
        raise ValueError("Invalid admitted journal scope")
    return owner.strip().casefold(), session_id, receipt


def _now(now: float | None) -> float:
    if now is None:
        return time.time()
    if type(now) not in (float, int) or not 0 < float(now) < 1e13:
        raise ValueError("Invalid journal timestamp")
    return float(now)


class NotesEventJournal:
    def __init__(self, engine: Engine):
        if not isinstance(engine, Engine) or engine.dialect.name not in {
            "sqlite", "postgresql",
        }:
            raise TypeError("Journal requires a trusted SQLite/PostgreSQL engine")
        self._engine = engine

    def require_schema(self) -> None:
        """Read-only schema validation; never migrate an application DB."""
        inspector = inspect(self._engine)
        if not inspector.has_table(_JOURNAL.name):
            raise RuntimeError("Notes journal migration is not installed")
        existing = {item["name"] for item in inspector.get_columns(_JOURNAL.name)}
        unique = {
            tuple(entry.get("column_names") or ())
            for entry in inspector.get_unique_constraints(_JOURNAL.name)
        }
        indexed = {entry["name"] for entry in inspector.get_indexes(_JOURNAL.name)}
        if (
            {column.name for column in _JOURNAL.columns} - existing
            or ("approval_id", "kind") not in unique
            or ("cursor_token",) not in unique
            or "ix_notes_journal_scope_cursor" not in indexed
            or "ix_notes_journal_retention" not in indexed
        ):
            raise RuntimeError("Incompatible notes journal schema")

    def append(
        self, *, owner: str, session_id: str, origin_user_receipt: str,
        approval_id: str, kind: JournalKind, now: float | None = None,
    ) -> str:
        """Write exactly one server-authorized milestone, with no payload.

        Does not authorize any decision. Duplicate milestones fail closed
        under a SQL unique constraint; no automatic replay/retry occurs.
        """
        owner, session_id, receipt = _identity(owner, session_id, origin_user_receipt)
        if (
            type(approval_id) is not str or _APPROVAL_RE.fullmatch(approval_id) is None
            or type(kind) is not str or kind not in _KINDS
        ):
            raise ValueError("Invalid server milestone")
        moment = _now(now)
        token = secrets.token_urlsafe(24)
        with self._engine.begin() as conn:
            conn.execute(insert(_JOURNAL).values(
                cursor_token=token,
                owner=owner, session_id=session_id,
                origin_user_receipt=receipt, approval_id=approval_id,
                kind=kind, created_at=moment,
            ))
        # Cursor is random and bound to scope in replay(), not an approval.
        return token

    def replay(
        self, *, owner: str, session_id: str, origin_user_receipt: str,
        after_cursor: str = "", limit: int = 50,
    ) -> tuple[NotesJournalEvent, ...]:
        """Bounded, owner+session+receipt scoped, read-only metadata playback.

        Never loads approval IDs or user/tool content into returned objects.
        The DB cursor is not an authorization token and is not an SSE cursor.
        """
        owner, session_id, receipt = _identity(owner, session_id, origin_user_receipt)
        if (
            type(after_cursor) is not str
            or (after_cursor and _CURSOR_RE.fullmatch(after_cursor) is None)
            or type(limit) is not int or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid journal pagination")
        scope = and_(
            _JOURNAL.c.owner == owner,
            _JOURNAL.c.session_id == session_id,
            _JOURNAL.c.origin_user_receipt == receipt,
        )
        with self._engine.connect() as conn:
            start = 0
            if after_cursor:
                row = conn.execute(
                    select(_JOURNAL.c.event_id)
                    .where(and_(scope, _JOURNAL.c.cursor_token == after_cursor))
                ).scalar_one_or_none()
                if row is None:
                    # An arbitrary/stale/foreign cursor must not fall back
                    # to the start of another person's timeline.
                    raise ValueError("Journal cursor does not belong to this session")
                start = row
            rows = conn.execute(
                select(
                    _JOURNAL.c.cursor_token,
                    _JOURNAL.c.kind,
                    _JOURNAL.c.created_at,
                )
                .where(and_(scope, _JOURNAL.c.event_id > start))
                .order_by(_JOURNAL.c.event_id)
                .limit(limit)
            ).all()
        return tuple(NotesJournalEvent(row[0], row[1], row[2]) for row in rows)

    def prune_terminal(
        self, *, older_than_seconds: int, now: float | None = None,
        max_rows: int = 1000,
    ) -> int:
        """Explicit retention only; never prune pending or uncertain actions.

        The irreversible decision fence remains untouched, even after journal
        pruning. The operator must approve retention and backup separately.
        """
        if (
            type(older_than_seconds) is not int
            or older_than_seconds < _MIN_RETENTION_SECONDS
            or type(max_rows) is not int or not 1 <= max_rows <= 1000
        ):
            raise ValueError("Unsafe journal retention limits")
        cutoff = _now(now) - older_than_seconds
        fence = notes_fence_metadata().tables["gateway_notes_decision_fences"]
        with self._engine.begin() as conn:
            ids = conn.execute(
                select(_JOURNAL.c.event_id)
                .join(fence, and_(
                    _JOURNAL.c.approval_id == fence.c.approval_id,
                    _JOURNAL.c.owner == fence.c.owner,
                    _JOURNAL.c.session_id == fence.c.session_id,
                    _JOURNAL.c.origin_user_receipt == fence.c.origin_user_receipt,
                ))
                .where(and_(
                    _JOURNAL.c.created_at <= cutoff,
                    fence.c.state.in_(("completed", "denied", "superseded")),
                ))
                .order_by(_JOURNAL.c.event_id)
                .limit(max_rows)
            ).scalars().all()
            if not ids:
                return 0
            result = conn.execute(
                delete(_JOURNAL).where(_JOURNAL.c.event_id.in_(ids))
            )
            return result.rowcount
