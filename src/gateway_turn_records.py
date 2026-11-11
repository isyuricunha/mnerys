"""M2.3 durable, owner-scoped turn/event/approval records.

An explicit client of the Gateway may write ordered, sanitized turn
events and seal approvals through this repository. The tables are never
auto-created; installation is the explicit migration. Payloads must be
bounded JSON from the sanitized typed adapter; raw provider data,
credentials, tool arguments/results and private identity are forbidden.
"""

from collections.abc import Callable, Iterator
from src.tool_approval_scopes import VALID_APPROVAL_DECISIONS
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "awaiting_approval", "approval_decided", "interrupted"})
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
        reject_existing: bool = False,
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
                if reject_existing:
                    raise TurnRecordError("Turn request was already admitted")
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
            from sqlalchemy.exc import IntegrityError
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if reject_existing and db.get(self.turn_model, turn_id) is not None:
                    raise TurnRecordError("Turn request was already admitted") from exc
                raise
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

    def finalize_with_event(self, *, turn_id: str, owner: str, status: str,
                            kind: str, payload: dict) -> bool:
        """Atomically append one terminal event and settle one running turn.

        The conditional UPDATE obtains the writer lock before sequence
        allocation on SQLite and PostgreSQL. A duplicate or foreign caller
        cannot append a second terminal. Failure rolls back both writes.
        """
        from sqlalchemy import func

        if status not in _TERMINAL_STATUSES or kind != {
            "completed": "turn.completed", "cancelled": "turn.cancelled",
            "failed": "turn.failed", "interrupted": "turn.failed",
            "awaiting_approval": "turn.awaiting_approval",
            "approval_decided": "turn.approval_decided",
        }[status]:
            raise TurnRecordError("Invalid terminal event/status")
        self._require_id_text("turn_id", turn_id)
        self._require_id_text("owner", owner)
        encoded = _safe_payload(payload)
        with self.session_factory() as db:
            matched = db.query(self.turn_model).filter(
                self.turn_model.id == turn_id, self.turn_model.owner == owner,
                self.turn_model.status == "running",
            ).update({self.turn_model.status: status,
                      self.turn_model.terminal_at: _utcnow()}, synchronize_session=False)
            if matched != 1:
                db.rollback()
                return False
            if status == "awaiting_approval" and payload.get("approval_id"):
                approval = db.get(self.approval_model, payload["approval_id"])
                if approval is not None and (approval.owner, approval.turn_id) == (owner, turn_id) \
                        and approval.status in {"consumed", "denied"}:
                    db.query(self.turn_model).filter(self.turn_model.id == turn_id).update(
                        {self.turn_model.status: "approval_decided"}, synchronize_session=False,
                    )
                    kind = "turn.approval_decided"
            sequence = (db.query(func.max(self.event_model.sequence)).filter(
                self.event_model.turn_id == turn_id,
            ).scalar() or 0) + 1
            db.add(self.event_model(id=uuid4().hex, turn_id=turn_id, sequence=sequence,
                                    kind=kind, payload=encoded))
            db.commit()
            return True

    def append_web_event(self, *, turn_id, owner, kind, payload) -> None:
        from sqlalchemy import func

        with self.session_factory() as db:
            matched = db.query(self.turn_model).filter(
                self.turn_model.id == turn_id, self.turn_model.owner == owner,
                self.turn_model.status == "running",
            ).update({self.turn_model.updated_at: _utcnow()}, synchronize_session=False)
            if matched != 1:
                raise TurnRecordError("Web turn is no longer running")
            seq = (db.query(func.max(self.event_model.sequence)).filter(
                self.event_model.turn_id == turn_id,
            ).scalar() or 0) + 1
            db.add(self.event_model(id=uuid4().hex, turn_id=turn_id, sequence=seq,
                                    kind=kind, payload=_safe_payload(payload)))
            db.commit()

    def interrupt_stale_turns(self, *, before: datetime, batch: int = 200) -> int:
        """Retire prior-process runs and approval projections, never replay tools.

        Effects may have happened before process loss. The terminal records
        explicitly retain that uncertainty. Only the trusted boot-time caller
        supplies the cutoff; current-process rows are never selected.
        """
        from sqlalchemy import func

        if not isinstance(before, datetime) or type(batch) is not int or not 1 <= batch <= 1000:
            raise TurnRecordError("Invalid restart reconciliation bound")
        count = 0
        with self.session_factory() as db:
            stale = db.query(self.turn_model).filter(
                self.turn_model.status.in_(("running", "awaiting_approval")),
                self.turn_model.created_at < before,
            ).order_by(self.turn_model.created_at, self.turn_model.id).limit(batch).all()
            for turn in stale:
                matched = db.query(self.turn_model).filter(
                    self.turn_model.id == turn.id, self.turn_model.owner == turn.owner,
                    self.turn_model.status.in_(("running", "awaiting_approval")),
                    self.turn_model.created_at < before,
                ).update({self.turn_model.status: "interrupted",
                          self.turn_model.terminal_at: _utcnow()}, synchronize_session=False)
                if matched != 1:
                    continue
                sequence = (db.query(func.max(self.event_model.sequence)).filter(
                    self.event_model.turn_id == turn.id,
                ).scalar() or 0) + 1
                db.add(self.event_model(id=uuid4().hex, turn_id=turn.id, sequence=sequence,
                    kind="turn.failed", payload=_safe_payload({
                        "code": "interrupted", "effects": "unknown", "retryable": False,
                    })))
                count += 1
            # No executable payload is stored here. All pending records from
            # the old process are unusable even if their display TTL remains.
            db.query(self.approval_model).filter(
                self.approval_model.status == "pending",
                self.approval_model.created_at < before,
                self.approval_model.expires_at.isnot(None),
                self.approval_model.expires_at <= _utcnow(),
            ).update({self.approval_model.status: "expired"}, synchronize_session=False)
            db.query(self.approval_model).filter(
                self.approval_model.status == "pending",
                self.approval_model.created_at < before,
            ).update({self.approval_model.status: "superseded"}, synchronize_session=False)
            db.commit()
        return count

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
        web_projection: bool = False,
    ) -> None:
        """Seal one approval; duplicate approval IDs are conflicts."""
        self._require_id_text("approval_id", approval_id)
        self._require_id(owner, session_id, turn_id)
        if not 1 <= len(tool_name) <= 128 or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", tool_name
        ):
            raise TurnRecordError("Invalid approval tool name")
        with self.session_factory() as db:
            if web_projection:
                from sqlalchemy import func
                if resource_identity is not None or effective_authority is not None:
                    raise TurnRecordError("Web approval projection cannot retain authority")
                matched = db.query(self.turn_model).filter(
                    self.turn_model.id == turn_id, self.turn_model.owner == owner,
                    self.turn_model.session_id == session_id, self.turn_model.incarnation_id == incarnation_id,
                    self.turn_model.status == "running", self.turn_model.policy_version == "web-recovery-v1",
                ).update({self.turn_model.updated_at: _utcnow()}, synchronize_session=False)
                if matched != 1:
                    raise TurnRecordError("Web approval has no matching running turn")
            if db.get(self.approval_model, approval_id) is not None:
                raise TurnRecordError("Approval record already exists")
            if web_projection:
                db.query(self.approval_model).filter(
                    self.approval_model.owner == owner, self.approval_model.session_id == session_id,
                    self.approval_model.status == "pending",
                ).update({self.approval_model.status: "superseded"}, synchronize_session=False)
                seq = (db.query(func.max(self.event_model.sequence)).filter(
                    self.event_model.turn_id == turn_id,
                ).scalar() or 0) + 1
                db.add(self.event_model(id=uuid4().hex, turn_id=turn_id, sequence=seq,
                    kind="approval.requested", payload=_safe_payload({"approval_id": approval_id})))
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
        if decision not in VALID_APPROVAL_DECISIONS:
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

    def expire_pending_approvals(self, *, owner: str | None = None, session_id: str | None = None) -> int:
        """Expire pending approvals past their deadline (bounded maintenance)."""
        with self.session_factory() as db:
            query = db.query(self.approval_model).filter(
                self.approval_model.status == "pending",
                self.approval_model.expires_at.isnot(None),
                self.approval_model.expires_at < _utcnow(),
            )
            if owner is not None:
                query = query.filter(self.approval_model.owner == owner)
            if session_id is not None:
                query = query.filter(self.approval_model.session_id == session_id)
            matched = query.update({
                self.approval_model.status: "expired",
            }, synchronize_session=False)
            db.commit()
            return matched

    def retire_session_approvals(self, *, owner: str, session_id: str) -> None:
        with self.session_factory() as db:
            query = db.query(self.approval_model).filter(
                self.approval_model.owner == owner, self.approval_model.session_id == session_id,
                self.approval_model.status == "pending",
            )
            query.update({self.approval_model.status: "superseded"}, synchronize_session=False)
            db.commit()

    def settle_web_approval(self, *, owner, session_id, incarnation_id, pending, decision) -> bool:
        """Record a decision from a consumed process-local seal, never issue one."""
        from sqlalchemy import func

        if decision not in VALID_APPROVAL_DECISIONS:
            raise TurnRecordError("Invalid approval decision")
        with self.session_factory() as db:
            row = db.get(self.approval_model, pending.approval_id)
            if row is None:
                return False  # Legacy approval without a durable projection.
            if (row.owner, row.session_id, row.incarnation_id, row.operation_digest) != (
                owner, session_id, incarnation_id, pending.digest,
            ):
                raise TurnRecordError("Approval projection does not match the seal")
            locked_turn = db.query(self.turn_model).filter(
                self.turn_model.id == row.turn_id, self.turn_model.owner == owner,
                self.turn_model.session_id == session_id,
                self.turn_model.incarnation_id == incarnation_id,
                self.turn_model.policy_version == "web-recovery-v1",
            ).update({self.turn_model.updated_at: _utcnow()}, synchronize_session=False)
            matched = db.query(self.approval_model).filter(
                self.approval_model.approval_id == pending.approval_id,
                self.approval_model.status == "pending",
            ).update({
                self.approval_model.status: "denied" if decision == "deny" else "consumed",
                self.approval_model.decision: decision,
                self.approval_model.consumed_at: _utcnow(),
            }, synchronize_session=False)
            if matched != 1:
                raise TurnRecordError("Approval projection is already settled")
            if locked_turn == 1:
                db.query(self.turn_model).filter(
                    self.turn_model.id == row.turn_id, self.turn_model.status == "awaiting_approval",
                ).update({self.turn_model.status: "approval_decided"}, synchronize_session=False)
                seq = (db.query(func.max(self.event_model.sequence)).filter(
                    self.event_model.turn_id == row.turn_id,
                ).scalar() or 0) + 1
                db.add(self.event_model(id=uuid4().hex, turn_id=row.turn_id, sequence=seq,
                    kind="approval.decided", payload=_safe_payload({
                        "approval_id": pending.approval_id, "decision": decision,
                    })))
            db.commit()
            return True

    def read_web_turn(self, *, owner, session_id, incarnation_id, turn_id=None):
        """Recover only the admitted incarnation's Web audit, never a raw SSE buffer."""
        with self.session_factory() as db:
            query = db.query(self.turn_model).filter(
                self.turn_model.owner == owner, self.turn_model.session_id == session_id,
                self.turn_model.incarnation_id == incarnation_id,
                self.turn_model.policy_version == "web-recovery-v1",
            )
            if turn_id is not None:
                query = query.filter(self.turn_model.id == turn_id)
            row = query.order_by(self.turn_model.created_at.desc(), self.turn_model.id.desc()).first()
            return self._turn_dict(row) if row is not None else None

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

    def list_session_approvals(
        self, *, owner: str, session_id: str, limit: int = 50,
    ) -> list[dict]:
        """Read owner-scoped approval projections for one session.

        Returns the display fields only: digests, resource identities and
        effective authority stay server-side. Newest first, bounded page.
        """
        self._require_id_text("owner", owner)
        self._require_id_text("session_id", session_id)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise TurnRecordError("Invalid approval page limit")
        self.expire_pending_approvals(owner=owner, session_id=session_id)
        with self.session_factory() as db:
            rows = db.query(self.approval_model).filter(
                self.approval_model.owner == owner,
                self.approval_model.session_id == session_id,
            ).order_by(self.approval_model.created_at.desc()).limit(limit).all()
            return [self._approval_dict(row) for row in rows]

    def prune_terminal_turns(self, *, before: datetime, batch: int = 200) -> int:
        """Delete terminal turns and their events older than ``before``."""
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise TurnRecordError("Invalid prune batch")
        with self.session_factory() as db:
            pending = db.query(self.approval_model).filter(
                self.approval_model.turn_id == self.turn_model.id,
                self.approval_model.status == "pending",
            ).exists()
            rows = db.query(self.turn_model).filter(
                self.turn_model.status.in_(_TERMINAL_STATUSES),
                self.turn_model.terminal_at.isnot(None),
                self.turn_model.terminal_at < before,
                ~pending,
            ).limit(batch).all()
            for row in rows:
                db.query(self.event_model).filter(self.event_model.turn_id == row.id).delete(synchronize_session=False)
                db.query(self.approval_model).filter(
                    self.approval_model.turn_id == row.id, self.approval_model.owner == row.owner,
                    self.approval_model.status.in_(_APPROVAL_TERMINALS),
                ).delete(synchronize_session=False)
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
    def _approval_dict(row) -> dict:
        """Display-only approval projection; never authority material."""

        def _iso(value: Any) -> str | None:
            return value.isoformat() if isinstance(value, datetime) else None

        return {
            "approval_id": row.approval_id,
            "turn_id": row.turn_id,
            "tool_name": row.tool_name,
            "status": row.status,
            "decision": row.decision,
            "created_at": _iso(row.created_at),
            "expires_at": _iso(row.expires_at),
            "consumed_at": _iso(row.consumed_at),
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
