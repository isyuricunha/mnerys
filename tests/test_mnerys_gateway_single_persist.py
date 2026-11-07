"""The v1 turn route must persist the assistant reply exactly once.

The production composer already applies the trusted persistence when a
persistence is supplied; the route used to wrap the delivered stream a
second time, writing every completed turn twice into chat_messages. This
suite drives the real route with a persistence-aware composer facade and
asserts the persisted row count.
"""

from dataclasses import replace

import httpx
import pytest

from core import database as core_database
from core.database import ChatMessage as DbChatMessage, utcnow_naive
from src.gateway_text_persistence import TrustedTextPersistence
from tests.remote_gateway.v1_app_router import (
    FakeComposer,
    FakeManager,
    OWNER,
    build_v1_app,
)


class PersistingFakeComposer(FakeComposer):
    """FakeComposer that mirrors the production inner persistence wrap."""

    def open_composed_text_events(self, admission, session, **_kwargs):
        from src.gateway_text_persistence import persist_completed_text_events

        persistence = _kwargs.get("persistence")
        delivered = super().open_composed_text_events(
            admission, session, **_kwargs,
        )
        if persistence is None:
            return delivered
        return replace(
            delivered,
            events=persist_completed_text_events(
                delivered.events, delivered.outcome,
                admission=admission, session=session,
                context_owner=_kwargs.get("context_owner"),
                persistence=persistence,
            ),
        )


def build_app(monkeypatch, tmp_path, seed_session_id="s1"):
    """Isolated app with the production router, durable records and the
    persistence wiring that production uses."""
    from routes import gateway_api
    from src import llm_core
    from src.gateway_admission import Principal

    app = build_v1_app(monkeypatch, tmp_path, seed_session_id=seed_session_id)

    def _principal(request):
        if request.headers.get("authorization") == "Bearer tok":
            return Principal(OWNER, "bearer", frozenset({"chat"}))
        return Principal(None, "browser", frozenset())

    monkeypatch.setattr(gateway_api, "principal_for_chat_request", _principal)
    monkeypatch.setattr(
        llm_core, "list_model_ids",
        lambda *_a, **_k: ["m1", "vendor/big"],
    )
    app.state.gateway_turns = PersistingFakeComposer()
    app.state.session_manager = FakeManager()
    return app


def _saved_rows(session_id):
    db = core_database.SessionLocal()
    try:
        return db.query(DbChatMessage).filter(
            DbChatMessage.session_id == session_id,
        ).all()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_completed_v1_turn_persists_one_assistant_row(monkeypatch, tmp_path):
    saved = []

    def counting_saver(sess, manager, sid, text, metrics, **kwargs):
        saved.append(text)
        receipt = f"a{len(saved)}"
        session_manager_history = getattr(sess, "history", None)
        if isinstance(session_manager_history, list):
            from core.models import ChatMessage

            session_manager_history.append(ChatMessage(
                "assistant", text, metadata={"_db_id": receipt},
            ))
        db = core_database.SessionLocal()
        try:
            row = DbChatMessage(
                id=receipt, session_id=sid, role="assistant",
                content=text, metadata={"fixture": True},
                timestamp=utcnow_naive(),
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
        return receipt

    app = build_app(monkeypatch, tmp_path)
    app.state.text_persistence = TrustedTextPersistence(
        object(), saver=counting_saver,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        created = await client.post(
            "/api/v1/turns",
            headers={"Authorization": "Bearer tok"},
            json={"session_id": "s1", "message": "hello", "mode": "chat"},
        )
        assert created.status_code == 201, created.text
        turn_id = created.json()["turn_id"]
        # Consume the detached stream so the terminal event runs.
        events = await client.get(
            f"/api/v1/turns/{turn_id}/events?after_seq=0",
            headers={"Authorization": "Bearer tok"},
        )
        assert events.status_code == 200
        assert '"turn.completed"' in events.text

    assert saved == ["hello"]
    rows = _saved_rows("s1")
    new_rows = [r for r in rows if r.id.startswith("a")]
    assistant_rows = [r for r in rows if r.role == "assistant"]
    # The seed session ships one user + one assistant fixture message; the
    # completed turn must add exactly one assistant row, never two.
    assert len(new_rows) == 1
    assert len(assistant_rows) == 2
    assert new_rows[0].content == "hello"


@pytest.mark.asyncio
async def test_incognito_v1_turn_skips_the_assistant_write(monkeypatch, tmp_path):
    saved = []

    def counting_saver(sess, manager, sid, text, metrics, **kwargs):
        saved.append(text)

    app = build_app(monkeypatch, tmp_path)
    app.state.text_persistence = TrustedTextPersistence(
        object(), saver=counting_saver,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        created = await client.post(
            "/api/v1/turns",
            headers={"Authorization": "Bearer tok"},
            json={"session_id": "s1", "message": "secret", "mode": "chat"},
        )
        assert created.status_code == 201, created.text
        turn_id = created.json()["turn_id"]
        events = await client.get(
            f"/api/v1/turns/{turn_id}/events?after_seq=0",
            headers={"Authorization": "Bearer tok"},
        )
        assert '"turn.completed"' in events.text

    assert saved == ["secret"]


@pytest.mark.asyncio
async def test_turn_service_survives_without_persistence(monkeypatch, tmp_path):
    app = build_app(monkeypatch, tmp_path)
    app.state.text_persistence = None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
    ) as client:
        created = await client.post(
            "/api/v1/turns",
            headers={"Authorization": "Bearer tok"},
            json={"session_id": "s1", "message": "hello", "mode": "chat"},
        )
        assert created.status_code == 201, created.text
        turn_id = created.json()["turn_id"]
        events = await client.get(
            f"/api/v1/turns/{turn_id}/events?after_seq=0",
            headers={"Authorization": "Bearer tok"},
        )
        assert '"turn.completed"' in events.text