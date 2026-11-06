"""M2.3 durable, owner-scoped turn/event/approval records.

An explicit client of the Gateway may write ordered, sanitized turn
events and seal approvals through this repository. The tables are never
auto-created; installation is the explicit migration. Payloads must be
bounded JSON from the sanitized typed adapter; raw provider data,
credentials, tool arguments/results and private identity are forbidden.
"""

from collections.abc import Callable, Iterator
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "awaiting_approval"})
_APPROVAL_TERMINALS = frozenset({"consumed", "denied", "expired", "superseded"})
_MAX_PAYLOAD_CHARS = 65536


class TurnRecordError(RuntimeError):
    """Orchestration error: ownership, state or sequencing violation."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_payload(payload: dict) -> str:
    if type(payload) is not dict:
        raise TurnRecordError("Event payload must be a JSON object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TurnRecordError("Event payload is not JSON serializable") from exc
    if len(encoded) > _MAX_PAYLOAD_CHARS:
        raise TurnRecordError("Event payload exceeds the bounded size")
    return encoded


class SqlTurnRecords:
    """Transactional SQL records for one admitted turn lifetime."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        turn_model: Any,
        event_model: Any,
        approval_model: Any,
    ) -> None:
        self.session_factory = session_factory
        self.turn_model = turn_model
        self.event_model = event_model
        self.approval_model = approval_model

    def open_turn(
        self,
        *,
        owner: str,
        session_id: str,
        incarnation_id: str,
        turn_id: str,
        idempotency_key: str | None = None,
        policy_version: str | None = None,
        cancellation_token: str | None = None,
    ) -> str:
        """Create one turn record, or return the matching existing one.

        An idempotency-key retry with the same turn ID reuses the stored
        record; the same key with a different turn ID is a conflict.
        """
        self._require_id(owner, session_id, turn_id)
        if idempotency_key is not None:
            self._require_id_text("idempotency_key", idempotency_key)
        with self.session_factory() as db:
            existing = db.get(self.turn_model, turn_id)
            if existing is not None:
                if existing.owner != owner or existing.session_id != session_id:
                    raise TurnRecordError("Turn record belongs to another owner/session")
                if idempotency_key is not None and existing.idempotency_key != idempotency_key:
                    raise TurnRecordError("Idempotency key reuse with a different turn")
                return turn_id
            if idempotency_key is not None:
                same_key = db.query(self.turn_model).filter(
                    self.turn_model.owner == owner,
                    self.turn_model.idempotency_key == idempotency_key,
                ).first()
                if same_key is not None:
                    raise TurnRecordError("Idempotency key reuse with a different turn")
            row = self.turn_model(
                id=turn_id, owner=owner, session_id=session_id,
                incarnation_id=incarnation_id,
                idempotency_key=idempotency_key,
                policy_version=policy_version,
                cancellation_token=cancellation_token,
                status="running",
            )
            db.add(row)
            db.commit()
            return turn_id

    def append_event(
        self,
        *,
        turn_id: str,
        owner: str,
        sequence: int,
        kind: str,
        payload: dict,
    ) -> int:
        """Append one sanitized event; sequence gaps are preserved.

        The unique (turn_id, sequence) constraint makes each sequence
        versioned and immutable; a gap left by a failed or filtered event
        is visible to readers as a missing sequence, never rewritten.
        """
        if type(sequence) is not int or sequence < 1 or sequence > 10_000_000:
            raise TurnRecordError("Invalid event sequence")
        if type(kind) is not str or not 1 <= len(kind) <= 64:
            raise TurnRecordError("Invalid event kind")
        encoded = _safe_payload(payload)
        with self.session_factory() as db:
            turn = db.get(self.turn_model, turn_id)
            if turn is None:
                raise TurnRecordError("Turn record does not exist")
            if turn.owner != owner:
                raise TurnRecordError("Turn record belongs to another owner")
            if turn.status != "running":
                raise TurnRecordError("Cannot append to a terminal turn")
            if db.query(self.event_model).filter(
                self.event_model.turn_id == turn_id,
                self.event_model.sequence == sequence,
            ).first() is not None:
                raise TurnRecordError("Event sequence already recorded")
            db.add(self.event_model(
                id=str(uuid4().hex), turn_id=turn_id, sequence=sequence,
                kind=kind, payload=encoded,
            ))
            db.commit()
            return sequence

    def finalize_turn(
        self, *, turn_id: str, owner: str, status: str,
    ) -> bool:
        """Move one running turn to a deterministic terminal exactly once."""
        if status not in _TERMINAL_STATUSES:
            raise TurnRecordError("Invalid terminal turn status")
        with self.session_factory() as db:
            matched = db.query(self.turn_model).filter(
                self.turn_model.id == turn_id,
                self.turn_model.owner == owner,
                self.turn_model.status == "running",
            ).update({
                self.turn_model.status: status,
                self.turn_model.terminal_at: _utcnow(),
            }, synchronize_session=False)
            db.commit()
            return matched == 1

    def record_approval(
        self,
        *,
        approval_id: str,
        owner: str,
        session_id: str,
        incarnation_id: str,
        turn_id: str,
        tool_name: str,
        operation_digest: str,
        resource_identity: str | None,
        effective_authority: str | None,
        expires_at: datetime | None,
    ) -> None:
        """Seal one approval; duplicate approval IDs are conflicts."""
        self._require_id_text("approval_id", approval_id)
        self._require_id(owner, session_id, turn_id)
        if not 1 <= len(tool_name) <= 128 or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", tool_name
        ):
            raise TurnRecordError("Invalid approval tool name")
        with self.session_factory() as db:
            if db.get(self.approval_model, approval_id) is not None:
                raise TurnRecordError("Approval record already exists")
            db.add(self.approval_model(
                approval_id=approval_id, owner=owner, session_id=session_id,
                incarnation_id=incarnation_id, turn_id=turn_id,
                tool_name=tool_name, operation_digest=operation_digest,
                resource_identity=resource_identity,
                effective_authority=effective_authority,
                status="pending", expires_at=expires_at,
            ))
            db.commit()

    def decide_approval(
        self, *, approval_id: str, owner: str, decision: str,
    ) -> bool:
        """Record one decision on a pending approval, exactly once."""
        if decision not in {"approve", "approve_task", "deny"}:
            raise TurnRecordError("Invalid approval decision")
        terminal = "consumed" if decision != "deny" else "denied"
        with self.session_factory() as db:
            matched = db.query(self.approval_model).filter(
                self.approval_model.approval_id == approval_id,
                self.approval_model.owner == owner,
                self.approval_model.status == "pending",
            ).update({
                self.approval_model.decision: decision,
                self.approval_model.status: terminal,
                self.approval_model.consumed_at: _utcnow(),
            }, synchronize_session=False)
            db.commit()
            return matched == 1

    def expire_pending_approvals(self, *, owner: str | None = None) -> int:
        """Expire pending approvals past their deadline (bounded maintenance)."""
        with self.session_factory() as db:
            query = db.query(self.approval_model).filter(
                self.approval_model.status == "pending",
                self.approval_model.expires_at.isnot(None),
                self.approval_model.expires_at < _utcnow(),
            )
            if owner is not None:
                query = query.filter(self.approval_model.owner == owner)
            matched = query.update({
                self.approval_model.status: "expired",
            }, synchronize_session=False)
            db.commit()
            return matched

    def read_turn(self, *, turn_id: str, owner: str) -> dict | None:
        with self.session_factory() as db:
            row = db.get(self.turn_model, turn_id)
            if row is None or row.owner != owner:
                return None
            return self._turn_dict(row)

    def iter_events(
        self,
        *,
        turn_id: str,
        owner: str,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> Iterator[dict]:
        """Read ordered, sanitized events with explicit allowed gaps."""
        with self.session_factory() as db:
            turn = db.get(self.turn_model, turn_id)
            if turn is None or turn.owner != owner:
                raise TurnRecordError("Turn record belongs to another owner or is missing")
            query = db.query(self.event_model).filter(
                self.event_model.turn_id == turn_id,
            )
            if after_sequence is not None:
                if type(after_sequence) is not int or after_sequence < 0:
                    raise TurnRecordError("Invalid after-sequence cursor")
                query = query.filter(self.event_model.sequence > after_sequence)
            query = query.order_by(self.event_model.sequence)
            if limit is not None:
                if type(limit) is not int or not 1 <= limit <= 1000:
                    raise TurnRecordError("Invalid event page limit")
                query = query.limit(limit)
            for row in query.all():
                yield {
                    "sequence": row.sequence,
                    "kind": row.kind,
                    "payload": json.loads(row.payload),
                }

    def prune_terminal_turns(self, *, before: datetime, batch: int = 200) -> int:
        """Delete terminal turns and their events older than ``before``."""
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise TurnRecordError("Invalid prune batch")
        with self.session_factory() as db:
            rows = db.query(self.turn_model).filter(
                self.turn_model.status.in_(_TERMINAL_STATUSES),
                self.turn_model.terminal_at.isnot(None),
                self.turn_model.terminal_at < before,
            ).limit(batch).all()
            for row in rows:
                db.delete(row)
            db.commit()
            return len(rows)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _turn_dict(self, row) -> dict:
        return {
            "id": row.id,
            "owner": row.owner,
            "session_id": row.session_id,
            "incarnation_id": row.incarnation_id,
            "idempotency_key": row.idempotency_key,
            "status": row.status,
            "policy_version": row.policy_version,
            "cancellation_token": row.cancellation_token,
            "terminal_at": row.terminal_at,
        }

    @staticmethod
    def _require_id(owner: str, session_id: str, turn_id: str) -> None:
        SqlTurnRecords._require_id_text("owner", owner)
        SqlTurnRecords._require_id_text("session_id", session_id)
        SqlTurnRecords._require_id_text("turn_id", turn_id)

    @staticmethod
    def _require_id_text(name: str, value: str) -> None:
        if type(value) is not str or _ID_PATTERN.fullmatch(value) is None:
            raise TurnRecordError(f"Invalid {name}")