"""M1 read-only session ownership port for Gateway admission.

The port is intentionally small: it returns only the owner required by the
admission policy. It is not the future full session CRUD/revision repository.
The SQL adapter accepts the already-configured session factory/model, so it
works with the existing SQLite/PostgreSQL setup without a new connection.
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol


@dataclass(frozen=True)
class SessionOwnership:
    """Owner of an existing persisted or in-memory session."""

    owner_id: Optional[str]


class SessionOwnershipLookup(Protocol):
    """Simple repository port; in-memory fixtures can supply a callable."""

    def __call__(self, session_id: str) -> Optional[SessionOwnership]:
        ...


@dataclass
class SqlSessionOwnershipLookup:
    """Read the DB first; allow a matching in-memory ghost only when supplied.

    The DB row is authoritative when both exist. Every opened DB session is
    closed, including query errors. Ghost sessions are process-local and are
    not a replacement for persistence or durable owner-scoped queries.
    """

    session_factory: Callable[[], Any]
    session_model: Any
    session_manager: Any = None

    def __call__(self, session_id: str) -> Optional[SessionOwnership]:
        db = self.session_factory()
        try:
            row = db.query(self.session_model.owner).filter(
                self.session_model.id == session_id
            ).first()
        finally:
            db.close()
        if row is not None:
            return SessionOwnership(owner_id=row.owner)
        if self.session_manager is not None:
            ghost = getattr(self.session_manager, "sessions", {}).get(session_id)
            if ghost is not None:
                return SessionOwnership(owner_id=getattr(ghost, "owner", None))
        return None
