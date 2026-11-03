"""M1 opt-in, revision-checked session metadata port (not deployed).

The live Session model has no revision column and older writers do not bump a
revision. Therefore this adapter is intentionally NOT wired to live routes.
It can only be used with a separately migrated model where every competing
writer participates in the same revision protocol. No schema is created here.

An UPDATE matching (session_id, owner, expected_revision) changes allowed
metadata and increments revision in ONE SQL transaction. Missing/stale/foreign
rows are all rejected without exposing their identity. It is not a durable
turn journal or a replacement for per-turn fencing.
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.gateway_admission import AdmittedSession, require_admitted_session


class SessionRevisionUnavailable(RuntimeError):
    """The repository lacks a verified, non-null revision column."""


class SessionRevisionConflict(RuntimeError):
    """Session was removed, changed, or is no longer owned by this principal."""


@dataclass(frozen=True)
class SessionMetadataChanges:
    """Explicit allowlisted mutable metadata; None means keep current value."""

    name: Optional[str] = None
    archived: Optional[bool] = None
    is_important: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.name is not None and (
            not isinstance(self.name, str) or not self.name.strip()
        ):
            raise ValueError("Session name must be non-empty")
        for flag in (self.archived, self.is_important):
            if flag is not None and not isinstance(flag, bool):
                raise ValueError("Session flags must be booleans")
        if self.name is None and self.archived is None and self.is_important is None:
            raise ValueError("A session metadata change is required")


@dataclass
class SqlSessionRevisionWriter:
    """Atomic owner-scoped compare-and-swap for a future migrated Session row.

    SQLAlchemy's Query.update is used deliberately: a direct SQL UPDATE with a
    revision predicate detects a stale writer by affected-row count. Mapper
    version_id_col alone does NOT protect bulk Query.update calls.
    """

    session_factory: Callable[[], Any]
    session_model: Any

    def _require_schema(self) -> None:
        if not hasattr(self.session_model, "revision"):
            raise SessionRevisionUnavailable(
                "Session revision schema is not available; migration required"
            )

    def _scoped_query(self, db: Any, admission: AdmittedSession):
        session_id = require_admitted_session(admission)
        q = db.query(self.session_model).filter(
            self.session_model.id == session_id
        )
        # Recheck ownership at write time; prior admission is not a DB lock.
        if admission.principal.owner_id is not None:
            q = q.filter(self.session_model.owner == admission.principal.owner_id)
        return q

    def current_revision(self, admission: AdmittedSession) -> Optional[int]:
        """Get the visible revision without reading another user's row."""
        require_admitted_session(admission)
        self._require_schema()
        db = self.session_factory()
        try:
            row = self._scoped_query(db, admission).with_entities(
                self.session_model.revision
            ).first()
            if row is None:
                return None
            revision = row[0]
            if type(revision) is not int or revision < 0:
                raise SessionRevisionUnavailable(
                    "Session revision is missing or invalid; backfill required"
                )
            return revision
        finally:
            db.close()

    def patch(
        self,
        admission: AdmittedSession,
        *,
        expected_revision: int,
        changes: SessionMetadataChanges,
    ) -> int:
        """Update fields + revision atomically; return the committed revision.

        This is an independent opt-in port, not enabled by current Web writes.
        A conflict performs no metadata write. The caller must re-read session
        state and decide how to reconcile, never retry blindly.
        """
        require_admitted_session(admission)
        self._require_schema()
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a non-negative integer")
        if not isinstance(changes, SessionMetadataChanges):
            raise TypeError("Expected SessionMetadataChanges")

        values = {self.session_model.revision: expected_revision + 1}
        if changes.name is not None:
            values[self.session_model.name] = changes.name
        if changes.archived is not None:
            values[self.session_model.archived] = changes.archived
        if changes.is_important is not None:
            values[self.session_model.is_important] = changes.is_important

        db = self.session_factory()
        try:
            matched = self._scoped_query(db, admission).filter(
                self.session_model.revision == expected_revision
            ).update(values, synchronize_session=False)
            if matched != 1:
                raise SessionRevisionConflict("Session revision conflict")
            db.commit()
            return expected_revision + 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
