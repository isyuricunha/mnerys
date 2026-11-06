"""Database-backed *decision fence* for a future durable notes_v1 lifecycle.

This is intentionally NOT an approval store, authorization mechanism, resume
adapter, background worker or tool executor. It never reconstructs a sealed
review or replays an effect. In particular, a reserved approval is **never**
automatically retried after a crash, regardless of whether its effect ran.

The canonical SQLAlchemy Core table contains metadata only: no tool input,
note body, model output, credentials or request text. Schema creation requires
an explicit call on an already-selected and isolated/migrated engine.

Future live admission must independently recheck the original signed pending
review, committed user-message receipt and interactive owner *before* trying
to reserve. Merely possessing an ID or a row does not authorize a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Literal

from sqlalchemy import (
    CheckConstraint, Column, Float, Index, MetaData, String, Table,
    and_, case, insert, inspect, select, text, update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

_PROFILE = "notes_v1"
_PENDING = "pending"
_RESERVED = "reserved"
_COMPLETED = "completed"
_DENIED = "denied"
_SUPERSEDED = "superseded"

_TERMINAL = frozenset({_COMPLETED, _DENIED, _SUPERSEDED})
# Usernames are validated by the application's account system, not by
# this internal SQL primitive. Preserve its Unicode owners without accepting
# control characters, empty identifiers or unbounded keys.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_RECEIPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_METADATA = MetaData()
_GATEWAY_NOTES_FENCES = Table(
    "gateway_notes_decision_fences", _METADATA,
    Column("approval_id", String(43), primary_key=True),
    Column("owner", String(255), nullable=False),
    Column("session_id", String(256), nullable=False),
    Column("origin_user_receipt", String(128), nullable=False),
    Column("action_digest", String(64), nullable=False),
    Column("profile", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("issued_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("assistant_receipt", String(128), nullable=True),
    CheckConstraint(
        "state IN ('pending', 'reserved', 'completed', 'denied', 'superseded')",
        name="ck_notes_fence_state",
    ),
    CheckConstraint("expires_at > issued_at", name="ck_notes_fence_ttl"),
    Index("ix_notes_fence_owner_session", "owner", "session_id", "state"),
    # Atomic cross-worker invariant: at most one pending notes review per
    # owner/session, on both supported databases. Reserved actions do not
    # block later unrelated requests, but can never be reactivated.
    Index(
        "ux_notes_fence_pending_session", "owner", "session_id", unique=True,
        sqlite_where=text("state = 'pending'"),
        postgresql_where=text("state = 'pending'"),
    ),
)


@dataclass(frozen=True)
class DecisionFenceStatus:
    """Only an owner-admitted internal status, never a grant or tool input."""

    state: Literal["pending", "reserved", "completed", "denied", "superseded", "expired"]
    expires_at: float
    assistant_receipt: str | None = None


def notes_fence_metadata() -> MetaData:
    """Return a reviewable schema; do not implicitly migrate any live DB."""
    return _METADATA


def _owner_value(owner: str) -> str:
    if (
        type(owner) is not str or not owner.strip()
        or len(owner) > 255 or any(ord(char) < 32 for char in owner)
    ):
        raise ValueError("Invalid admitted notes owner identity")
    return owner.strip().casefold()


def _require_identity(owner: str, session_id: str, approval_id: str) -> tuple[str, str, str]:
    normalized_owner = _owner_value(owner)
    if (
        type(session_id) is not str or not _SESSION_ID.fullmatch(session_id)
        or type(approval_id) is not str or not _APPROVAL_ID.fullmatch(approval_id)
    ):
        raise ValueError("Invalid admitted notes fence identity")
    return normalized_owner, session_id, approval_id


def _require_seal(digest: str, receipt: str) -> tuple[str, str]:
    if (
        type(digest) is not str or not _DIGEST.fullmatch(digest)
        or type(receipt) is not str or not _RECEIPT.fullmatch(receipt)
    ):
        raise ValueError("Invalid verified notes fence seal or receipt")
    return digest, receipt


def _timestamp(now: float | None) -> float:
    if now is None:
        return time.time()
    if type(now) not in (int, float) or not (0 < float(now) < 1e13):
        raise ValueError("Invalid notes fence timestamp")
    return float(now)


class NotesDecisionFence:
    """Transactional metadata-only one-way decision fence.

    The caller must select a trusted engine whose schema was installed by a
    reviewed migration. This class NEVER calls create_all or mutates schemas.
    """

    def __init__(self, engine: Engine):
        if not isinstance(engine, Engine) or engine.dialect.name not in {
            "sqlite", "postgresql",
        }:
            raise TypeError("Notes fence requires a supported trusted database engine")
        self._engine = engine

    def require_schema(self) -> None:
        """Fail closed before a new request if the reviewed schema is absent.

        This is read-only validation, *not* a startup auto-migration. An
        operator must explicitly install the schema in the correct database.
        """
        inspector = inspect(self._engine)
        table = _GATEWAY_NOTES_FENCES
        if not inspector.has_table(table.name):
            raise RuntimeError("Notes decision fence migration is not installed")
        columns = {col["name"] for col in inspector.get_columns(table.name)}
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes(table.name)
        }
        expected = {col.name for col in table.columns}
        guard = indexes.get("ux_notes_fence_pending_session")
        if (
            not expected <= columns
            or guard is None
            or guard.get("unique") not in (True, 1)
        ):
            raise RuntimeError("Notes decision fence schema is incompatible")

    def issue(
        self, *, owner: str, session_id: str, approval_id: str,
        action_digest: str, origin_user_receipt: str,
        issued_at: float, expires_at: float,
        now: float | None = None,
    ) -> bool:
        """Record only an independently verified pending review before display.

        A duplicate ID fails closed. New pending reviews retire previously
        pending reviews in the same owner/session inside this transaction.
        No decision can be reactivated by issue(), including after expiry.
        """
        owner, session_id, approval_id = _require_identity(owner, session_id, approval_id)
        action_digest, origin_user_receipt = _require_seal(
            action_digest, origin_user_receipt,
        )
        if (
            type(issued_at) not in (int, float)
            or type(expires_at) not in (int, float)
            or not (0 < float(issued_at) <= float(expires_at) <= float(issued_at) + 600)
            or float(expires_at) <= float(issued_at)
        ):
            raise ValueError("Invalid notes review expiration")
        current = _timestamp(now)
        if float(expires_at) <= current:
            return False
        try:
            with self._engine.begin() as conn:
                # The unique ID is the only insert identity; do not allow
                # reuse even if its previous state was terminal.
                existing = conn.execute(select(_GATEWAY_NOTES_FENCES.c.approval_id).where(
                    _GATEWAY_NOTES_FENCES.c.approval_id == approval_id,
                )).first()
                if existing is not None:
                    return False
                conn.execute(update(_GATEWAY_NOTES_FENCES).where(and_(
                    _GATEWAY_NOTES_FENCES.c.owner == owner,
                    _GATEWAY_NOTES_FENCES.c.session_id == session_id,
                    _GATEWAY_NOTES_FENCES.c.state == _PENDING,
                )).values(state=_SUPERSEDED, updated_at=current))
                conn.execute(insert(_GATEWAY_NOTES_FENCES).values(
                    approval_id=approval_id,
                    owner=owner,
                    session_id=session_id,
                    origin_user_receipt=origin_user_receipt,
                    action_digest=action_digest,
                    profile=_PROFILE,
                    state=_PENDING,
                    issued_at=float(issued_at),
                    expires_at=float(expires_at),
                    updated_at=current,
                ))
            return True
        except IntegrityError:
            # A racing identical issue never re-enables an old decision.
            return False

    def reserve(
        self, *, owner: str, session_id: str, approval_id: str,
        action_digest: str, origin_user_receipt: str,
        decision: Literal["approve", "approve_task", "deny"],
        now: float | None = None,
    ) -> bool:
        """Atomically reserve ONE decision; a lost process leaves no replay.

        A successful approval changes pending -> reserved BEFORE any tool
        action. A denial changes pending -> denied. Only the caller's
        separately authenticated, sealed approval may choose the decision.
        """
        owner, session_id, approval_id = _require_identity(owner, session_id, approval_id)
        action_digest, origin_user_receipt = _require_seal(
            action_digest, origin_user_receipt,
        )
        if type(decision) is not str or decision not in {
            "approve", "approve_task", "deny",
        }:
            raise ValueError("Invalid notes decision")
        current = _timestamp(now)
        state = _DENIED if decision == "deny" else _RESERVED
        with self._engine.begin() as conn:
            result = conn.execute(update(_GATEWAY_NOTES_FENCES).where(and_(
                _GATEWAY_NOTES_FENCES.c.owner == owner,
                _GATEWAY_NOTES_FENCES.c.session_id == session_id,
                _GATEWAY_NOTES_FENCES.c.approval_id == approval_id,
                _GATEWAY_NOTES_FENCES.c.origin_user_receipt == origin_user_receipt,
                _GATEWAY_NOTES_FENCES.c.action_digest == action_digest,
                _GATEWAY_NOTES_FENCES.c.profile == _PROFILE,
                _GATEWAY_NOTES_FENCES.c.state == _PENDING,
                _GATEWAY_NOTES_FENCES.c.expires_at > current,
            )).values(state=state, updated_at=current))
            return result.rowcount == 1

    def mark_committed(
        self, *, owner: str, session_id: str, approval_id: str,
        action_digest: str, origin_user_receipt: str,
        assistant_receipt: str, now: float | None = None,
    ) -> bool:
        """Mark completed only after a verified original assistant DB commit.

        The reservation still cannot be reissued or executed if this call is
        lost after a crash. The committed status is informational only.
        """
        owner, session_id, approval_id = _require_identity(owner, session_id, approval_id)
        action_digest, origin_user_receipt = _require_seal(
            action_digest, origin_user_receipt,
        )
        if type(assistant_receipt) is not str or not _RECEIPT.fullmatch(assistant_receipt):
            raise ValueError("Invalid confirmed assistant receipt")
        current = _timestamp(now)
        with self._engine.begin() as conn:
            result = conn.execute(update(_GATEWAY_NOTES_FENCES).where(and_(
                _GATEWAY_NOTES_FENCES.c.owner == owner,
                _GATEWAY_NOTES_FENCES.c.session_id == session_id,
                _GATEWAY_NOTES_FENCES.c.approval_id == approval_id,
                _GATEWAY_NOTES_FENCES.c.origin_user_receipt == origin_user_receipt,
                _GATEWAY_NOTES_FENCES.c.action_digest == action_digest,
                _GATEWAY_NOTES_FENCES.c.profile == _PROFILE,
                _GATEWAY_NOTES_FENCES.c.state == _RESERVED,
            )).values(
                state=_COMPLETED,
                updated_at=current,
                assistant_receipt=assistant_receipt,
            ))
            return result.rowcount == 1

    def retire_pending(
        self, *, owner: str, session_id: str, now: float | None = None,
    ) -> int:
        """Supersede only pending cards; never unlock reserved actions."""
        owner = _owner_value(owner)
        if type(session_id) is not str or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid notes session identity")
        current = _timestamp(now)
        with self._engine.begin() as conn:
            result = conn.execute(update(_GATEWAY_NOTES_FENCES).where(and_(
                _GATEWAY_NOTES_FENCES.c.owner == owner,
                _GATEWAY_NOTES_FENCES.c.session_id == session_id,
                _GATEWAY_NOTES_FENCES.c.state == _PENDING,
            )).values(state=_SUPERSEDED, updated_at=current))
            return result.rowcount

    def read_only_recovery(
        self, *, owner: str, session_id: str,
        origin_user_receipt: str, now: float | None = None,
    ) -> Literal["interrupted", "indeterminate", "expired"] | None:
        """Read metadata for a paused review after its process-local run is gone.

        The caller must separately verify the interactive session owner and
        compare origin_user_receipt to the actual latest committed *user*
        message. Only the non-actionable classification is returned. No
        approval ID, tool input, digest, authority or replay cursor escapes.

        A reserved action takes priority over a still-pending review for the
        same original receipt: after an uncertain effect, never imply that an
        operator can safely grant the original action again.
        """
        normalized_owner = _owner_value(owner)
        if (
            type(session_id) is not str or not _SESSION_ID.fullmatch(session_id)
            or type(origin_user_receipt) is not str
            or not _RECEIPT.fullmatch(origin_user_receipt)
        ):
            raise ValueError("Invalid notes recovery context")
        current = _timestamp(now)
        row = _GATEWAY_NOTES_FENCES
        with self._engine.connect() as conn:
            state = conn.execute(
                select(row.c.state, row.c.expires_at)
                .where(and_(
                    row.c.owner == normalized_owner,
                    row.c.session_id == session_id,
                    row.c.origin_user_receipt == origin_user_receipt,
                    row.c.profile == _PROFILE,
                    row.c.state.in_((_PENDING, _RESERVED)),
                ))
                .order_by(
                    case((row.c.state == _RESERVED, 0), else_=1),
                    row.c.issued_at.desc(),
                    row.c.approval_id.desc(),
                )
                .limit(1)
            ).first()
        if state is None:
            return None
        phase, expiry = state
        if phase == _RESERVED:
            return "indeterminate"
        return "expired" if expiry <= current else "interrupted"

    def status(
        self, *, owner: str, session_id: str,
        approval_id: str, now: float | None = None,
    ) -> DecisionFenceStatus | None:
        """Read-only owner-scoped metadata. No card, grant or content escapes."""
        owner, session_id, approval_id = _require_identity(owner, session_id, approval_id)
        current = _timestamp(now)
        with self._engine.connect() as conn:
            record = conn.execute(select(
                _GATEWAY_NOTES_FENCES.c.state,
                _GATEWAY_NOTES_FENCES.c.expires_at,
                _GATEWAY_NOTES_FENCES.c.assistant_receipt,
            ).where(and_(
                _GATEWAY_NOTES_FENCES.c.owner == owner,
                _GATEWAY_NOTES_FENCES.c.session_id == session_id,
                _GATEWAY_NOTES_FENCES.c.approval_id == approval_id,
                _GATEWAY_NOTES_FENCES.c.profile == _PROFILE,
            ))).first()
        if record is None:
            return None
        state, expires_at, receipt = record
        return DecisionFenceStatus(
            "expired" if state == _PENDING and expires_at <= current else state,
            expires_at=expires_at, assistant_receipt=receipt,
        )
