# core/session_manager.py
"""
Session management — all session business logic and DB operations.

This is the single place that handles:
- Loading/saving sessions to database
- Adding messages to sessions
- Session lifecycle (create, archive, delete)
"""

import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func

from .database import Session as DbSession, ChatMessage as DbChatMessage, Document as DbDocument, SessionLocal, utcnow_naive
from .models import Session, ChatMessage
from src.session_message_preparation import prepare_session_message
from src.upload_handler import reserve_message_upload_references

# Re-export singleton accessors from models for convenience
from .models import set_session_manager_instance, get_session_manager_instance

logger = logging.getLogger(__name__)

# M2.2 write fencing: any in-flight session mutation must re-check the
# persisted owner/revision/incarnation before committing. See
# docs/architecture/SESSION_REVISION_MIGRATION.md and the stop condition
# there: never mix a revisioned writer with an unguarded one on a
# migrated schema.


class SessionRevisionConflict(RuntimeError):
    """A competing writer committed first; the caller's write is refused."""


def _session_fencing_columns_present() -> bool:
    """Whether the mapped Session model carries the fencing columns."""
    return hasattr(DbSession, "revision") and hasattr(DbSession, "incarnation_id")


def _message_timestamp_iso(value: Optional[datetime]) -> Optional[str]:
    """Return a stable ISO timestamp for chat message metadata."""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _parse_msg_content(raw):
    """Parse message content from DB — deserialises JSON arrays back to lists
    (multimodal content with image/audio attachments)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.startswith('[{') and '"type"' in raw:
        try:
            parsed = json.loads(raw)
            # Only treat as serialized multimodal content when EVERY element is
            # a dict whose "type" is a recognized content-block kind. Otherwise a
            # plain text message that merely *looks* like a JSON array of objects
            # (e.g. a user pasting an API schema/sample with a "type" field) was
            # silently parsed back into a list, destroying the original string.
            _BLOCK_TYPES = {
                "text", "image", "image_url", "audio", "input_audio",
                "input_image", "document", "file",
            }
            if (isinstance(parsed, list) and parsed
                    and all(isinstance(p, dict) and p.get("type") in _BLOCK_TYPES
                            for p in parsed)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


class SessionManager:
    """
    Manages chat sessions with database persistence.

    Usage:
        manager = SessionManager()
        session = manager.create_session(id, name, url, model)
        manager.add_message(session.id, ChatMessage("user", "hello"))
        session = manager.get_session(session_id)
    """

    def __init__(self, sessions_file: str = None):
        # sessions_file kept for backward compat, not used
        self.sessions: Dict[str, Session] = {}
        self.upload_handler = None
        self.load_sessions()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_sessions(self):
        """Load recent session METADATA from the database — messages are
        hydrated on demand by `get_session`. Previously this walked every
        message of every session into RAM at boot, which on a long-running
        personal-server box could be tens of thousands of rows held forever
        in `self.sessions`.
        """
        db = SessionLocal()
        try:
            db_sessions = db.query(DbSession).filter(
                DbSession.archived == False,
                DbSession.messages.any(),
            ).order_by(DbSession.last_accessed.desc()).limit(100).all()

            # message_count is derived metadata and can drift after interrupted
            # or legacy writes. Count only the bounded discovery set so startup
            # remains metadata-only while lazy hydration sees an authoritative
            # positive count for every discovered non-empty session.
            message_counts = {}
            if db_sessions:
                message_counts = dict(
                    db.query(DbChatMessage.session_id, func.count(DbChatMessage.id))
                    .filter(DbChatMessage.session_id.in_([row.id for row in db_sessions]))
                    .group_by(DbChatMessage.session_id)
                    .all()
                )

            loaded_count = 0
            for db_session in db_sessions:
                try:
                    session = self._db_to_session_meta(db_session)
                    if session is not None:
                        session.message_count = message_counts[db_session.id]
                        self.sessions[db_session.id] = session
                        loaded_count += 1
                except Exception as e:
                    logger.error(f"Error loading session {db_session.id}: {e}")
                    continue

            logger.info(f"Loaded {loaded_count} session(s) (metadata only)")

        except Exception as e:
            logger.error(f"Error loading sessions: {e}")
            self.sessions = {}
        finally:
            db.close()

    def _db_to_session_meta(self, db_session: DbSession) -> Optional[Session]:
        """Build a Session with empty history. `get_session` will hydrate
        messages from the DB on first read."""
        headers = db_session.headers
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                headers = {}
        session = Session(
            id=db_session.id,
            name=db_session.name,
            endpoint_url=db_session.endpoint_url,
            model=db_session.model,
            rag=db_session.rag,
            archived=db_session.archived,
            headers=headers,
            history=[],
            owner=getattr(db_session, "owner", None),
            is_important=getattr(db_session, "is_important", False) or False,
            memory_extraction_enabled=getattr(db_session, "memory_extraction_enabled", True) is not False,
            memory_injection_enabled=getattr(db_session, "memory_injection_enabled", True) is not False,
            skill_injection_enabled=getattr(db_session, "skill_injection_enabled", True) is not False,
            thinking_mode=getattr(db_session, "thinking_mode", "") or "off",
            temperature_override=getattr(db_session, "temperature_override", None),
            max_tokens_override=getattr(db_session, "max_tokens_override", None),
            cwd=getattr(db_session, "cwd", None) or None,
            endpoint_id=getattr(db_session, "endpoint_id", None) or None,
            revision=getattr(db_session, "revision", None),
            incarnation_id=getattr(db_session, "incarnation_id", None),
        )
        session.message_count = getattr(db_session, "message_count", 0) or 0
        return session

    def _db_to_session(self, db_session: DbSession, db) -> Optional[Session]:
        """Convert a database session to a Session object."""
        history = []

        # Try relationship first, then direct query
        if db_session.messages:
            for db_msg in db_session.messages:
                meta = json.loads(db_msg.meta_data) if db_msg.meta_data else {}
                if meta is None: meta = {}
                meta['_db_id'] = db_msg.id
                meta.setdefault('timestamp', _message_timestamp_iso(db_msg.timestamp))
                history.append(ChatMessage(
                    role=db_msg.role,
                    content=_parse_msg_content(db_msg.content),
                    metadata=meta,
                ))
        else:
            db_messages = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == db_session.id
            ).order_by(DbChatMessage.timestamp).all()

            for db_msg in db_messages:
                meta = json.loads(db_msg.meta_data) if db_msg.meta_data else {}
                if meta is None: meta = {}
                meta['_db_id'] = db_msg.id
                meta.setdefault('timestamp', _message_timestamp_iso(db_msg.timestamp))
                history.append(ChatMessage(
                    role=db_msg.role,
                    content=_parse_msg_content(db_msg.content),
                    metadata=meta,
                ))

        if not history:
            return None

        # Parse headers
        headers = db_session.headers
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                headers = {}

        session = Session(
            id=db_session.id,
            name=db_session.name,
            endpoint_url=db_session.endpoint_url,
            model=db_session.model,
            rag=db_session.rag,
            archived=db_session.archived,
            headers=headers,
            history=history,
            owner=getattr(db_session, 'owner', None),
            is_important=getattr(db_session, 'is_important', False) or False,
            memory_extraction_enabled=getattr(db_session, 'memory_extraction_enabled', True) is not False,
            memory_injection_enabled=getattr(db_session, 'memory_injection_enabled', True) is not False,
            skill_injection_enabled=getattr(db_session, 'skill_injection_enabled', True) is not False,
            thinking_mode=getattr(db_session, "thinking_mode", "") or "off",
            temperature_override=getattr(db_session, "temperature_override", None),
            max_tokens_override=getattr(db_session, "max_tokens_override", None),
            cwd=getattr(db_session, "cwd", None) or None,
            endpoint_id=getattr(db_session, "endpoint_id", None) or None,
            revision=getattr(db_session, "revision", None),
            incarnation_id=getattr(db_session, "incarnation_id", None),
        )

        # The rows just loaded are the whole transcript, so they — not the
        # denormalized sessions.message_count column — are the truth for this
        # cached object. get_session's hydration gate compares against this
        # number; seeding it from a drifted column would ask for a reload that
        # can never close the gap.
        session.message_count = len(history)
        return session

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def _apply_session_changes(
        self,
        db,
        session_id: str,
        changes: Dict[Any, Any],
        *,
        expected_owner: Optional[str] = None,
        expected_incarnation: Optional[str] = None,
        bump_revision: bool = True,
    ) -> bool:
        """Apply one metadata update under the revision/incarnation fence.

        Unmigrated rows (revision or incarnation NULL) keep the legacy
        unguarded write until the explicit migration backfills them; every
        migrated row is updated in one guarded UPDATE keyed by
        id + owner + incarnation + revision, so a competing writer makes the
        update match zero rows and this write is refused instead of losing
        updates. When the caller still holds a cached incarnation that no
        longer matches the row (deleted and recreated session ID), the
        stale writer is refused outright. Unmapped fencing columns keep
        the legacy behavior.
        """
        if not _session_fencing_columns_present():
            for column, value in changes.items():
                setattr(
                    db.query(DbSession).filter(DbSession.id == session_id).first(),
                    getattr(column, "key", column), value,
                )
            return True
        db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
        if db_session is None:
            return False
        incarnation = getattr(db_session, "incarnation_id", None)
        revision = getattr(db_session, "revision", None)
        # Strict native types only: a mocked or legacy row (NULL fence
        # columns) keeps the unguarded write instead of pretending a fence.
        if not (type(incarnation) is str and incarnation and type(revision) is int):
            for column, value in changes.items():
                setattr(db_session, getattr(column, "key", column), value)
            return True
        if (
            type(expected_incarnation) is str and expected_incarnation
            and expected_incarnation != incarnation
        ):
            raise SessionRevisionConflict(
                f"Session {session_id} was recreated; stale writer refused"
            )
        query = db.query(DbSession).filter(
            DbSession.id == session_id,
            DbSession.incarnation_id == incarnation,
            DbSession.revision == revision,
        )
        if expected_owner is not None:
            query = query.filter(DbSession.owner == expected_owner)
        update_values = dict(changes)
        if bump_revision:
            update_values[DbSession.revision] = revision + 1
        matched = query.update(update_values, synchronize_session=False)
        if matched != 1:
            raise SessionRevisionConflict(
                f"Session {session_id} changed concurrently; write refused"
            )
        return True

    def add_message(self, session_id: str, message: ChatMessage):
        """
        Add a message to a session and persist to database.

        Updates the authoritative history list and persists through this
        manager directly so tests and temporary managers do not depend on the
        process-wide session-manager singleton.

        Args:
            session_id: Session ID
            message: ChatMessage to add
        """
        session = self.get_session(session_id)
        session.history.append(message)
        session._history = session.history
        session.message_count = len(session.history)

        if self._persist_message(session_id, message) is False:
            session._discard_failed_message(message)

    def _persist_message(self, session_id: str, message: ChatMessage) -> bool:
        """Persist one message; report commit success to the caller's cache.

        False denotes a failed or refused write, not a committed message.
        The return value is intentionally a boolean for the inherited
        Session.add_message compatibility adapter, not a revision receipt.
        """
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                # A stream/tool callback can outlive a session delete. Do not
                # create a chat_messages row with no parent session; also drop
                # any stale cached session so later writes fail closed too.
                self.sessions.pop(session_id, None)
                logger.warning("Dropping message for deleted session %s", session_id)
                return False

            # A loaded session may outlive an ownership transfer. Never append
            # using a stale in-memory owner after the database owner changed.
            # This is a compatibility guard, not a cross-process revision fence.
            cached_session = self.sessions.get(session_id)
            if (
                cached_session is not None
                and getattr(cached_session, "owner", None) != db_session.owner
            ):
                logger.warning("Dropping message for a session with changed ownership")
                return False

            missing_upload_id = reserve_message_upload_references(
                getattr(self, "upload_handler", None),
                getattr(db_session, "owner", None),
                message.content,
                message.metadata,
            )
            if missing_upload_id:
                raise ValueError(
                    f"Referenced upload is no longer available: {missing_upload_id}"
                )

            msg_id = str(uuid.uuid4())
            msg_time = datetime.utcnow()
            if message.metadata is None:
                message.metadata = {}
            message.metadata.setdefault('timestamp', _message_timestamp_iso(msg_time))
            # The common server-side preparation boundary is also used by
            # background delivery and the future fenced writer. Uploads were
            # already reserved above, before any persistent mutation.
            prepared = prepare_session_message(
                message.role, message.content, message.metadata,
            )
            db_message = DbChatMessage(
                id=msg_id,
                session_id=session_id,
                role=prepared.role,
                content=prepared.content,
                meta_data=prepared.metadata_json,
                timestamp=msg_time,
            )
            db.add(db_message)

            _now = datetime.now(timezone.utc)
            # One fenced transaction: the message insert and the guarded
            # counter/timestamp/revision update commit together. A competing
            # writer (or an owner transfer) refuses this append instead of
            # losing its update.
            self._apply_session_changes(db, session_id, {
                DbSession.message_count: (
                    len(self.sessions[session_id].history)
                    if session_id in self.sessions else 0
                ),
                DbSession.last_accessed: _now,
                DbSession.last_message_at: _now,
            }, expected_owner=getattr(cached_session, "owner", None),
                expected_incarnation=getattr(cached_session, "incarnation_id", None))

            db.commit()

            # Store DB ID on the in-memory message for edit/delete by ID
            message.metadata['_db_id'] = msg_id

            logger.debug(f"Persisted message to session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Error persisting message: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def truncate_messages(self, session_id: str, keep_count: int) -> bool:
        """Truncate session history, keeping only the first `keep_count` messages."""
        session = self.get_session(session_id)

        if keep_count < 0:
            return False

        db = SessionLocal()
        try:
            db_messages = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == session_id
            ).order_by(DbChatMessage.timestamp).all()

            deleted = 0
            for msg in db_messages[keep_count:]:
                db.delete(msg)
                deleted += 1

            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                # keep_count can exceed the real message total (e.g. the AI tool
                # defaults to keep_count=10 on a short session); message_count must
                # track the rows that actually remain, not the requested cap.
                self._apply_session_changes(db, session_id, {
                    DbSession.message_count: min(keep_count, len(db_messages)),
                    DbSession.updated_at: datetime.now(timezone.utc),
                })

            db.commit()

            # Update in-memory
            session.history = session.history[:keep_count]
            session._history = session.history

            logger.info(f"Truncated session {session_id} to {keep_count} messages")
            return True

        except Exception as e:
            logger.error(f"Error truncating session: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def replace_messages(self, session_id: str, messages: list) -> bool:
        """Replace a session's persisted and in-memory history atomically."""
        session = self.get_session(session_id)
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                logger.warning("Cannot replace history for missing session %s", session_id)
                return False

            # Reserve every incoming attachment before removing any durable
            # message row. reserve_upload() shares the upload lifecycle lock
            # with cleanup, so an upload cannot be deleted between this
            # ownership check/access touch and the replacement transaction.
            # A failed reservation must leave the existing transcript intact.
            for message in messages:
                missing_upload_id = reserve_message_upload_references(
                    getattr(self, "upload_handler", None),
                    getattr(db_session, "owner", None),
                    message.content,
                    message.metadata,
                )
                if missing_upload_id:
                    raise ValueError(
                        f"Referenced upload is no longer available: {missing_upload_id}"
                    )

            db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).delete()
            now = datetime.now(timezone.utc)
            committed_message_ids = []
            for i, message in enumerate(messages):
                msg_id = str(uuid.uuid4())
                # The reservation loop above must finish before deleting the
                # previous transcript. Preparation does not reserve twice.
                prepared = prepare_session_message(
                    message.role, message.content, message.metadata,
                )
                db_message = DbChatMessage(
                    id=msg_id,
                    session_id=session_id,
                    role=prepared.role,
                    content=prepared.content,
                    meta_data=prepared.metadata_json,
                    timestamp=now + timedelta(microseconds=i),
                )
                db.add(db_message)
                # Do not expose IDs to caller-owned objects before commit.
                committed_message_ids.append((message, msg_id))

            _now = datetime.now(timezone.utc)
            self._apply_session_changes(db, session_id, {
                DbSession.message_count: len(messages),
                DbSession.updated_at: _now,
                DbSession.last_accessed: _now,
                DbSession.last_message_at: _now,
            })

            db.commit()
            for message, msg_id in committed_message_ids:
                if message.metadata is None:
                    message.metadata = {}
                message.metadata["_db_id"] = msg_id
            session.history = list(messages)
            session._history = session.history
            session.message_count = len(messages)
            logger.info("Replaced session %s history with %d messages", session_id, len(messages))
            return True
        except Exception as e:
            logger.error("Error replacing session history: %s", e)
            db.rollback()
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Session:
        """Get a session by ID, loading complete DB history when needed.

        Sessions seeded by ``load_sessions`` start with empty history, and a
        cached session can also become partially stale. Refresh metadata first,
        then hydrate whenever the cached transcript is short of the stored rows.
        Model-send routes enter through this method before building context,
        while paginated display history reads SQLite directly.

        The gate compares against ``sync_session_metadata``'s reconciled count
        (the real ``chat_messages`` total), never the denormalized column, so a
        hydrate always closes the gap and the next read is a cache hit.
        """
        if session_id not in self.sessions:
            self._load_session_from_db(session_id)

        # Keep model/endpoint metadata fresh. Endpoint deletion can clear the
        # DB row while a session object is still cached in RAM. Refreshing first
        # also exposes the authoritative message count before completeness is
        # checked.
        self.sync_session_metadata(session_id)

        cached = self.sessions[session_id]
        cached_count = len(cached.history or [])
        stored_count = int(getattr(cached, "message_count", 0) or 0)
        if cached_count < stored_count:
            self._load_session_from_db(session_id)

        # Update last_accessed
        self._touch_session(session_id)

        return self.sessions[session_id]

    def sync_session_metadata(self, session_id: str) -> bool:
        """Refresh non-message session fields from the DB into the cached object.

        ``message_count`` is reconciled against the real ``chat_messages`` rows
        rather than copied from the denormalized ``sessions.message_count``
        column. That column can still drift after legacy or direct writers,
        especially when persisting an uncached session. Failed writes through
        the inherited append paths now roll back their in-memory message, but
        historical and independent writers do not share a versioned counter.
        Hydration must therefore use the actual persisted message total.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return False
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                return False
            headers = db_session.headers
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except json.JSONDecodeError:
                    headers = {}
            session.name = db_session.name
            session.endpoint_url = db_session.endpoint_url or ""
            session.endpoint_id = getattr(db_session, "endpoint_id", None) or None
            session.model = db_session.model or ""
            session.headers = headers or {}
            session.rag = db_session.rag
            session.archived = db_session.archived
            session.owner = getattr(db_session, "owner", None)
            session.is_important = getattr(db_session, "is_important", False) or False
            session.cwd = getattr(db_session, "cwd", None) or None
            # Keep the cached fence in sync with the persisted lifetime.
            session.revision = getattr(db_session, "revision", None)
            session.incarnation_id = getattr(db_session, "incarnation_id", None)
            session.message_count = (
                db.query(DbChatMessage)
                .filter(DbChatMessage.session_id == session_id)
                .count()
            )
            return True
        except Exception as e:
            logger.error(f"Error syncing session metadata {session_id}: {e}")
            return False
        finally:
            db.close()

    def _load_session_from_db(self, session_id: str):
        """Hydrate a single session (with messages) from the database."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                raise KeyError(f"Session {session_id} not found")

            session = self._db_to_session(db_session, db)
            if session:
                self.sessions[session_id] = session
            else:
                # No messages — fall back to metadata-only entry so callers
                # don't crash on KeyError for empty sessions.
                meta = self._db_to_session_meta(db_session)
                if meta is None:
                    raise KeyError(f"Session {session_id} could not be loaded")
                self.sessions[session_id] = meta

        except KeyError:
            raise
        except Exception as e:
            logger.error(f"Error loading session {session_id}: {e}")
            raise
        finally:
            db.close()

    def _touch_session(self, session_id: str):
        """Update last_accessed timestamp under the incarnation fence."""
        db = SessionLocal()
        try:
            if not _session_fencing_columns_present():
                db_session = db.query(DbSession).filter(
                    DbSession.id == session_id
                ).first()
                if db_session:
                    db_session.last_accessed = datetime.now(timezone.utc)
                    db.commit()
                return
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                if db_session.incarnation_id is None:
                    # Unmigrated legacy row.
                    db_session.last_accessed = datetime.now(timezone.utc)
                    db.commit()
                    return
                # A cosmetic touch never bumps the revision: it must not
                # conflict with real writes, but it must never touch a
                # recreated session lifetime either.
                matched = db.query(DbSession).filter(
                    DbSession.id == session_id,
                    DbSession.incarnation_id == db_session.incarnation_id,
                ).update({
                    DbSession.last_accessed: datetime.now(timezone.utc),
                }, synchronize_session=False)
                if matched != 1:
                    logger.warning(
                        "Skipping touch for replaced session %s", session_id,
                    )
                db.commit()
        except Exception as e:
            logger.error(f"Error updating last_accessed: {e}")
            db.rollback()
        finally:
            db.close()

    def create_session(
        self,
        session_id: str,
        name: str,
        endpoint_url: str,
        model: str,
        rag: bool = False,
        owner: str = None,
        cwd: str = None,
        headers: Optional[Dict[str, str]] = None,
        endpoint_id: Optional[str] = None,
    ) -> Session:
        """Create a new session and save to database."""
        from src.chatgpt_subscription import is_chatgpt_subscription_base
        session_headers = {} if is_chatgpt_subscription_base(endpoint_url) else dict(headers or {})
        endpoint_id = (endpoint_id or "").strip() or None
        db = SessionLocal()
        try:
            incarnation_id = uuid.uuid4().hex
            db_session = DbSession(
                id=session_id,
                name=name,
                endpoint_url=endpoint_url,
                model=model,
                rag=rag,
                headers=session_headers,
                owner=owner,
                cwd=cwd or None,
                endpoint_id=endpoint_id,
                revision=0,
                incarnation_id=incarnation_id,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            db.add(db_session)
            db.commit()

            session = Session(
                id=session_id,
                name=name,
                endpoint_url=endpoint_url,
                model=model,
                rag=rag,
                headers=session_headers,
                owner=owner,
                cwd=cwd or None,
                endpoint_id=endpoint_id,
                revision=0,
                incarnation_id=incarnation_id,
            )

            self.sessions[session_id] = session
            return session

        except Exception as e:
            db.rollback()
            logger.error(f"Error creating session: {e}")
            raise
        finally:
            db.close()

    def delete_session(self, session_id: str, *, delete_images: bool = False) -> bool:
        """Permanently delete a session and all its messages."""
        db = SessionLocal()
        try:
            try:
                from src.session_image_cleanup import cleanup_session_images, preserve_session_images
                if delete_images:
                    cleanup_session_images(session_id, db=db)
                else:
                    preserve_session_images(session_id, db=db)
            except Exception as e:
                logger.warning(f"Image cleanup failed while deleting session {session_id}: {e}")

            # Detach documents so they survive as orphans in the library
            db.query(DbDocument).filter(DbDocument.session_id == session_id).update(
                {DbDocument.session_id: None}, synchronize_session=False
            )

            # Delete messages
            db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).delete()

            # Delete session
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                db.delete(db_session)

            # Drop the in-memory copy even when there is no DB row. A "ghost"
            # session lives only here (never persisted, or its row was removed
            # out-of-band); without this it can never be cleared and keeps
            # 404ing on every operation (issue #1044).
            removed_in_memory = self.sessions.pop(session_id, None) is not None

            if db_session or removed_in_memory:
                # Commit the document-detach / message-delete above (a no-op when
                # the ghost had no rows) together with the session delete.
                db.commit()
                logger.info(f"Deleted session {session_id}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error deleting session: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Session updates
    # ------------------------------------------------------------------

    def update_session_name(self, session_id: str, name: str):
        """Update session name under the revision fence."""
        if session_id not in self.sessions:
            return

        db = SessionLocal()
        try:
            self._apply_session_changes(db, session_id, {
                DbSession.name: name,
                DbSession.updated_at: datetime.now(timezone.utc),
            }, expected_incarnation=getattr(
                self.sessions[session_id], "incarnation_id", None,
            ))
            db.commit()
            self.sessions[session_id].name = name
        except SessionRevisionConflict as e:
            db.rollback()
            logger.error(f"Refused stale session rename: {e}")
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating session name: {e}")
            raise
        finally:
            db.close()

    def archive_session(self, session_id: str):
        """Archive a session under the revision fence."""
        if session_id not in self.sessions:
            return

        db = SessionLocal()
        try:
            self._apply_session_changes(db, session_id, {
                DbSession.archived: True,
                DbSession.updated_at: datetime.now(timezone.utc),
            }, expected_incarnation=getattr(
                self.sessions[session_id], "incarnation_id", None,
            ))
            db.commit()
            self.sessions[session_id].archived = True
        except SessionRevisionConflict as e:
            db.rollback()
            logger.error(f"Refused stale session archive: {e}")
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Error archiving session: {e}")
            raise
        finally:
            db.close()

    def mark_important(self, session_id: str, important: bool = True):
        """Mark session as important under the revision fence."""
        db = SessionLocal()
        try:
            applied = self._apply_session_changes(db, session_id, {
                DbSession.is_important: important,
                DbSession.updated_at: datetime.now(timezone.utc),
            })
            if not applied:
                raise KeyError(f"Session {session_id} not found")
            db.commit()

            if session_id in self.sessions:
                self.sessions[session_id].is_important = important
        except Exception as e:
            db.rollback()
            logger.error(f"Error marking session important: {e}")
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_sessions_for_user(self, username: Optional[str] = None) -> Dict[str, Session]:
        """Return sessions for a specific user (or all if username is None)."""
        if username is None:
            return self.sessions
        return {
            sid: s for sid, s in self.sessions.items()
            if s.owner == username
        }

    def save_sessions(self):
        """No-op for DB compatibility."""

    def ensure_task_session(self, session_id: str, name: str, endpoint_url: str, model: str, owner: str = None, task: object = None) -> Session:
        """Create a task session if it doesn't exist, or return the existing one.

        Unlike create_session, this checks the cache first and does NOT
        overwrite an existing in-memory session. The task scheduler must
        use this instead of direct dict assignment.
        """
        if session_id in self.sessions:
            return self.sessions[session_id]

        session = self.create_session(session_id, name, endpoint_url, model, owner=owner)
        if task is not None:
            task.session_id = session_id
        return session

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_empty_sessions(self, auto_archive_days: int = 30, min_age_hours: int = 1) -> dict:
        """Clean up empty and old sessions.

        Args:
            auto_archive_days: Age in days before non-important sessions are archived.
            min_age_hours: Minimum age in hours before an empty session can be deleted.
                          Prevents deleting sessions that were just created.
        """
        db = SessionLocal()
        stats = {'deleted_empty': 0, 'archived_old': 0, 'total_checked': 0}

        try:
            all_sessions = db.query(DbSession).all()
            cutoff_date = utcnow_naive() - timedelta(days=auto_archive_days)
            min_age = utcnow_naive() - timedelta(hours=min_age_hours)

            for db_session in all_sessions:
                stats['total_checked'] += 1

                # Delete empty sessions only if older than min_age_hours
                if db_session.message_count == 0:
                    if db_session.created_at is not None:
                        created = db_session.created_at
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        if created > min_age:
                            continue  # Too young to delete
                    # Fenced delete: a message that arrived after this
                    # cleanup read bumped the row's revision/counter, so the
                    # guarded DELETE matches nothing and the session
                    # survives with its new message.
                    delete_query = db.query(DbSession).filter(
                        DbSession.id == db_session.id,
                        DbSession.message_count == 0,
                    )
                    if _session_fencing_columns_present():
                        delete_query = delete_query.filter(
                            DbSession.incarnation_id == db_session.incarnation_id,
                            DbSession.revision == db_session.revision,
                        )
                    if delete_query.delete(synchronize_session=False) != 1:
                        logger.info(
                            "Cleanup skipped session %s: it changed concurrently",
                            db_session.id,
                        )
                        continue
                    if db_session.id in self.sessions:
                        del self.sessions[db_session.id]
                    stats['deleted_empty'] += 1

                # Archive old sessions
                elif (not db_session.archived and
                      db_session.last_accessed and
                      db_session.last_accessed < cutoff_date and
                      db_session.message_count > 0 and
                      not getattr(db_session, 'is_important', False)):
                    self._apply_session_changes(db, db_session.id, {
                        DbSession.archived: True,
                    })
                    stats['archived_old'] += 1

            db.commit()
            logger.info(f"Cleanup: {stats['deleted_empty']} deleted, {stats['archived_old']} archived")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            db.rollback()
            raise
        finally:
            db.close()

        return stats
