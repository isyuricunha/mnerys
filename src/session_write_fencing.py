"""M1 test-only, incarnation-fenced session mutation repository.

This prototype is intentionally NOT connected to the current SessionManager.
It requires a future schema with non-null revision and incarnation_id columns,
and every competing writer must participate before it can be activated.

A row-version check alone permits ABA after a deleted session ID is recreated.
The opaque incarnation binds a writer to one lifetime of a session ID.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy import func

from src.gateway_admission import AdmittedSession, require_admitted_session
from src.session_revision import (
    SessionMetadataChanges,
    SessionRevisionConflict,
    SessionRevisionUnavailable,
)


@dataclass(frozen=True)
class SessionWriteFence:
    """Database-issued snapshot of a session incarnation and revision."""

    session_id: str
    incarnation_id: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("Session ID is required")
        if not isinstance(self.incarnation_id, str) or not self.incarnation_id.strip():
            raise ValueError("Session incarnation is required")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Session revision must be a non-negative integer")


@dataclass(frozen=True)
class PreparedSessionMessage:
    """Pre-sanitized persistence payload from the trusted runtime adapter.

    Callers MUST use existing upload validation and persistable_message_content
    before constructing this object. This object is not an API request model:
    a future runtime adapter will own the conversion, not a Web/CLI client.
    """

    role: str
    content: str
    metadata_json: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Message role is required")
        if not isinstance(self.content, str):
            raise ValueError("Prepared message content must be text")
        if self.metadata_json is not None and not isinstance(self.metadata_json, str):
            raise ValueError("Serialized metadata must be text or None")


@dataclass
class SqlFencedSessionWriter:
    """Owner- and incarnation-scoped transactional SQLAlchemy writer.

    This is an unactivated adapter for a verified migrated schema. No table is
    created and no database is connected until an explicit method is called.
    """

    session_factory: Callable[[], Any]
    session_model: Any
    message_model: Any

    def _require_schema(self) -> None:
        session_columns = (
            "id", "owner", "revision", "incarnation_id", "name",
            "archived", "is_important", "message_count",
            "last_accessed", "last_message_at",
        )
        message_columns = (
            "id", "session_id", "role", "content", "meta_data", "timestamp",
        )
        if (
            any(not hasattr(self.session_model, column) for column in session_columns)
            or any(not hasattr(self.message_model, column) for column in message_columns)
        ):
            raise SessionRevisionUnavailable(
                "Fenced session schema unavailable; migration required"
            )

    def _query(self, db: Any, admission: AdmittedSession):
        session_id = require_admitted_session(admission)
        query = db.query(self.session_model).filter(
            self.session_model.id == session_id
        )
        if admission.principal.owner_id is not None:
            query = query.filter(
                self.session_model.owner == admission.principal.owner_id
            )
        return query

    def read_fence(self, admission: AdmittedSession) -> Optional[SessionWriteFence]:
        """Read a session's current fence, without exposing a foreign row."""
        require_admitted_session(admission)
        self._require_schema()
        db = self.session_factory()
        try:
            row = self._query(db, admission).with_entities(
                self.session_model.revision,
                self.session_model.incarnation_id,
            ).first()
            if row is None:
                return None
            revision, incarnation_id = row
            if (
                type(revision) is not int or revision < 0
                or not isinstance(incarnation_id, str) or not incarnation_id.strip()
            ):
                raise SessionRevisionUnavailable(
                    "Session revision or incarnation needs backfill"
                )
            return SessionWriteFence(
                require_admitted_session(admission), incarnation_id, revision
            )
        finally:
            db.close()

    def _guard(self, db: Any, admission: AdmittedSession, fence: SessionWriteFence):
        session_id = require_admitted_session(admission)
        if not isinstance(fence, SessionWriteFence) or fence.session_id != session_id:
            raise SessionRevisionConflict("Session write fence mismatch")
        return self._query(db, admission).filter(
            self.session_model.incarnation_id == fence.incarnation_id,
            self.session_model.revision == fence.revision,
        )

    def _write(self, admission, fence, values, *, prepared=None):
        require_admitted_session(admission)
        self._require_schema()
        if not isinstance(fence, SessionWriteFence):
            raise SessionRevisionConflict("Session write fence mismatch")
        if fence.session_id != admission.session_id:
            raise SessionRevisionConflict("Session write fence mismatch")
        next_revision = fence.revision + 1
        changes = {self.session_model.revision: next_revision, **values}
        db = self.session_factory()
        try:
            matched = self._guard(db, admission, fence).update(
                changes, synchronize_session=False
            )
            if matched != 1:
                raise SessionRevisionConflict("Session revision conflict")
            message_id = None
            if prepared is not None:
                message_id = uuid4().hex
                db.add(self.message_model(
                    id=message_id,
                    session_id=fence.session_id,
                    role=prepared.role,
                    content=prepared.content,
                    meta_data=prepared.metadata_json,
                    timestamp=datetime.now(timezone.utc),
                ))
            db.commit()
            return SessionWriteFence(fence.session_id, fence.incarnation_id, next_revision), message_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def patch_metadata(
        self,
        admission: AdmittedSession,
        fence: SessionWriteFence,
        changes: SessionMetadataChanges,
    ) -> SessionWriteFence:
        require_admitted_session(admission)
        self._require_schema()
        if not isinstance(changes, SessionMetadataChanges):
            raise TypeError("Expected SessionMetadataChanges")
        values = {}
        if changes.name is not None:
            values[self.session_model.name] = changes.name
        if changes.archived is not None:
            values[self.session_model.archived] = changes.archived
        if changes.is_important is not None:
            values[self.session_model.is_important] = changes.is_important
        result, _ = self._write(admission, fence, values)
        return result

    def append_prepared_message(
        self,
        admission: AdmittedSession,
        fence: SessionWriteFence,
        message: PreparedSessionMessage,
    ) -> tuple[SessionWriteFence, str]:
        """Atomically update session revision/counter and insert one message."""
        require_admitted_session(admission)
        self._require_schema()
        if not isinstance(message, PreparedSessionMessage):
            raise TypeError("Expected PreparedSessionMessage")
        now = datetime.now(timezone.utc)
        next_fence, message_id = self._write(
            admission,
            fence,
            {
                self.session_model.message_count:
                    func.coalesce(self.session_model.message_count, 0) + 1,
                self.session_model.last_accessed: now,
                self.session_model.last_message_at: now,
            },
            prepared=message,
        )
        return next_fence, message_id
