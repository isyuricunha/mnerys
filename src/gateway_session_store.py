"""Owner-scoped session storage for the Gateway v1 routes (M4.1).

The router stays a transport/admission surface and never touches the
database; every read and write here goes through the configured SQLAlchemy
session factory and keeps rows owner-scoped. Session creation and model
hints are validated server-side against the registered endpoints: raw
endpoint URLs and API keys are never accepted on this surface.
"""

import json
from typing import Any

from core import database as core_database
from core.database import (
    ChatMessage as DbChatMessage,
    ModelEndpoint,
    Session as DbSession,
    utcnow_naive,
)

HIDDEN_SYSTEM_SESSION_NAMES = {
    "[Task] Chat Sessions Tidy",
    "[Task] Documents Tidy",
    "[Task] Memory Tidy",
    "[Task] Research Tidy",
    "[Task] Email Mark Boundaries",
    "[Task] Email Tags",
    "[Task] Skills Audit",
}


class SessionStoreError(RuntimeError):
    """A store rule rejected the operation; ``status_code`` maps to HTTP."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def is_hidden_session_name(name: str | None) -> bool:
    """Return whether a session should be omitted from client listings."""
    clean = (name or "").strip()
    return (
        clean in ("Nobody", "Incognito")
        or clean in HIDDEN_SYSTEM_SESSION_NAMES
        or clean.startswith("SFT trace batch ")
    )


def _db():
    """Open one session from the configured application factory."""
    return core_database.SessionLocal()


def list_owner_sessions(owner: str) -> list[dict]:
    """Complete owner-scoped session metadata, newest activity first.

    The database is the authoritative listing; the in-memory manager cache
    is only a bounded recent subset. Raises SessionStoreError when the
    durable rows cannot be read so callers can fall back explicitly.
    """
    from src.auth_helpers import owner_filter

    db = _db()
    try:
        try:
            query = owner_filter(
                db.query(DbSession).filter(DbSession.archived == False),
                DbSession, owner,
            ).order_by(DbSession.is_important.desc(), DbSession.updated_at.desc())
            rows = query.all()
        except Exception as exc:
            raise SessionStoreError(
                "the durable session listing is unavailable",
                status_code=503,
            ) from exc
        items = []
        for row in rows:
            if is_hidden_session_name(row.name):
                continue
            items.append({
                "id": row.id,
                "name": row.name,
                "model": row.model,
                "mode": getattr(row, "mode", None) or "chat",
            })
        return items
    finally:
        db.close()


def session_history_page(session_id: str, *, limit: int, offset: int) -> dict:
    """One persisted, sanitized history page for an owner-checked session."""
    from routes.chat_helpers import strip_tui_local_context
    from routes.history.history_routes import _history_display_content

    db = _db()
    try:
        total = (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == session_id)
            .count()
        )
        page_limit = max(1, min(int(limit), 500))
        page_offset = max(0, min(int(offset), total))
        rows = (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == session_id)
            .order_by(DbChatMessage.timestamp)
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )
        history = []
        for row in rows:
            meta: dict = {}
            if row.meta_data:
                try:
                    meta = json.loads(row.meta_data) or {}
                except (ValueError, TypeError):
                    meta = {}
            if meta.get("hidden"):
                continue
            history.append({
                "role": row.role,
                "content": strip_tui_local_context(_history_display_content(row.content)),
                "metadata": meta,
            })
        return {
            "history": history,
            "total": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more_before": page_offset > 0,
            "has_more_after": page_offset + len(rows) < total,
        }
    finally:
        db.close()


def resolve_endpoint_row(owner: str, endpoint_id: str | None) -> ModelEndpoint:
    """One enabled, owner-filtered ModelEndpoint row (explicit or default)."""
    from src.auth_helpers import owner_filter

    db = _db()
    try:
        query = owner_filter(
            db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True),
            ModelEndpoint, owner,
        )
        if endpoint_id:
            row = query.filter(ModelEndpoint.id == endpoint_id).first()
            if row is None:
                raise SessionStoreError(
                    "Model endpoint no longer exists", status_code=400,
                )
            return row
        row = query.order_by(ModelEndpoint.updated_at.desc()).first()
        if row is None:
            raise SessionStoreError(
                "No registered model endpoint for this owner", status_code=400,
            )
        return row
    finally:
        db.close()


def validated_session_model(endpoint_row: Any, owner: str, model_hint: str | None) -> str:
    """Validate the model hint against the endpoint registry, server-side."""
    from src.endpoint_resolver import build_chat_url, build_headers, normalize_base
    from src.llm_core import list_model_ids

    endpoint_url = build_chat_url(normalize_base(endpoint_row.base_url or ""))
    headers = build_headers(endpoint_row.api_key, endpoint_row.base_url or "") if (
        endpoint_row.api_key
    ) else None
    available = list_model_ids(
        endpoint_url, timeout=3, headers=headers, owner=owner,
        endpoint_id=getattr(endpoint_row, "id", None),
    )
    if not available:
        raise SessionStoreError("Cannot reach /v1/models", status_code=400)
    if model_hint:
        base_hint = model_hint.rsplit("/", 1)[-1]
        for candidate in available:
            if candidate == model_hint or candidate.rsplit("/", 1)[-1] == base_hint:
                return candidate
        raise SessionStoreError(
            f"Model not found at server. Available: {', '.join(available[:20])}",
            status_code=400,
        )
    non_chat = (
        "text-embedding", "embedding", "tts-", "whisper", "text-moderation",
        "moderation-", "dall-e", "rerank",
    )
    chat_ids = [m for m in available if not any(p in m.lower() for p in non_chat)]
    return (chat_ids or available)[0]


def create_owner_session(
    manager: Any, owner: str, *, name: str,
    endpoint_id: str | None, model_hint: str | None,
    cwd: str | None = None,
) -> Any:
    """Create one owner-scoped session from a registered endpoint.

    ``cwd`` (M5.3 increment 1) is the session's working directory: a
    client-supplied value is validated server-side (``vetted_session_cwd``)
    and an invalid selection is refused; when the caller does not choose one,
    the natural default applies — the execution account's HOME (local CLI/TUI
    clients pass their invoking shell ``pwd`` explicitly).
    """
    from datetime import timezone
    from uuid import uuid4

    from src.endpoint_resolver import build_chat_url, build_headers, normalize_base

    endpoint_row = resolve_endpoint_row(owner, endpoint_id)
    model = validated_session_model(endpoint_row, owner, model_hint)
    endpoint_url = build_chat_url(normalize_base(endpoint_row.base_url or ""))
    # Model validation and turn execution must use the same server-owned
    # endpoint credentials. The Web session route already persists these
    # headers; omitting them here makes Gateway-created turns fail with 401.
    headers = (
        build_headers(endpoint_row.api_key, endpoint_row.base_url or "")
        if endpoint_row.api_key else {}
    )
    session_cwd = None
    if cwd is not None and str(cwd).strip():
        from src.session_cwd import vetted_session_cwd

        session_cwd, reason = vetted_session_cwd(cwd)
        if session_cwd is None:
            raise SessionStoreError(f"Invalid working directory: {reason}", status_code=400)
    else:
        # Natural default at creation (M5.3 increment 1): the execution
        # account's HOME for authorized callers; local CLI/TUI clients pass
        # their invoking shell pwd explicitly. No binding for callers without
        # the workspace-backed tools.
        from src.tool_security import owner_is_admin_or_single_user

        if owner_is_admin_or_single_user(owner):
            from src.session_cwd import default_session_cwd_for_surface

            session_cwd, reason = default_session_cwd_for_surface("cli", remote=False)
            if session_cwd is None and cwd is not None:
                raise SessionStoreError(
                    f"Invalid working directory: {reason}", status_code=400,
                )
    session = manager.create_session(
        session_id=str(uuid4()),
        name=name or "",
        endpoint_url=endpoint_url,
        model=model,
        owner=owner,
        endpoint_id=getattr(endpoint_row, "id", None),
        headers=headers,
        cwd=session_cwd,
    )
    return session


def update_owner_session(
    manager: Any, session: Any, owner: str, *, name: str | None = None,
    endpoint_id: str | None = None, model_hint: str | None = None,
    cwd: str | None = None,
) -> Any:
    """Apply owner-checked preference updates to one admitted session.

    ``cwd`` (M5.3 increment 1): a non-empty value is validated server-side
    and becomes the session's authoritative working directory; an empty
    string clears the selection. Invalid selections are refused and the
    previous directory is kept.
    """
    from src.endpoint_resolver import build_chat_url, build_headers, normalize_base

    if name:
        manager.update_session_name(session.id, name)
    if model_hint or endpoint_id:
        endpoint_row = resolve_endpoint_row(owner, endpoint_id)
        endpoint_url = build_chat_url(normalize_base(endpoint_row.base_url or ""))
        model = validated_session_model(endpoint_row, owner, model_hint)
        session.model = model
        session.endpoint_url = endpoint_url
        session.endpoint_id = getattr(endpoint_row, "id", None)
        session.headers = (
            build_headers(endpoint_row.api_key, endpoint_row.base_url or "")
            if endpoint_row.api_key else {}
        )
        if getattr(session, "thinking_mode", "").startswith("effort:"):
            from src.chatgpt_subscription import get_chatgpt_model_metadata

            meta = get_chatgpt_model_metadata(model)
            current_effort = session.thinking_mode[7:]
            if not meta or current_effort not in [
                lvl.lower() for lvl in meta.get("supported_reasoning_levels", [])
            ]:
                session.thinking_mode = "off"
        db = _db()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session.id).first()
            if db_session is not None:
                db_session.model = model
                db_session.endpoint_url = endpoint_url
                db_session.endpoint_id = session.endpoint_id
                db_session.headers = session.headers or {}
                db_session.thinking_mode = getattr(session, "thinking_mode", "off") or "off"
                db_session.updated_at = utcnow_naive()
                db.commit()
        finally:
            db.close()
    if cwd is not None:
        from src.session_cwd import clearable_session_cwd

        clean_cwd, cwd_reason = clearable_session_cwd(cwd)
        if cwd and clean_cwd is None:
            raise SessionStoreError(
                f"Invalid working directory: {cwd_reason}", status_code=400,
            )
        db = _db()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session.id).first()
            if db_session is not None:
                db_session.cwd = clean_cwd
                db_session.updated_at = utcnow_naive()
                db.commit()
                session.cwd = clean_cwd
        finally:
            db.close()
    return session
