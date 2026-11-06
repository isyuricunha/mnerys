"""M4.1 integration: the real Gateway v1 router serves TUI session surfaces.

The session creation, owner-scoped detail/history, approval inspection and
session preference endpoints run against the production router mounted on a
small test app with a real temporary SQLite database and the real durable
record store. Nothing here touches real user sessions.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as core_database
from core.database import (
    Base,
    ChatMessage as DbChatMessage,
    GatewayApproval,
    GatewayTurn,
    GatewayTurnEvent,
    ModelEndpoint,
    Session as DbSession,
)
from routes import gateway_api
from routes.gateway_api import router as gateway_router
from src.gateway_admission import Principal
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedTurnDelivery
from src.gateway_turn_records import SqlTurnRecords
from src.turn_service import TurnService

OWNER = "alice"
OTHER = "mallory"
TOKEN = "Bearer test-token"


def _principal(request):
    if request.headers.get("authorization") == TOKEN:
        return Principal(OWNER, "bearer", frozenset({"chat"}))
    return Principal(None, "browser", frozenset())


gateway_api.principal_for_chat_request = _principal


class FakeManager:
    """The manager surface the v1 routes need, backed by the real DB."""

    def __init__(self):
        self.sessions: dict = {}

    def get_session(self, session_id: str):
        row = self._db_row(session_id)
        if row is None:
            raise KeyError(session_id)
        return self.sessions.get(session_id) or SimpleNamespace(
            id=row.id, name=row.name, model=row.model,
            endpoint_url=row.endpoint_url, endpoint_id=row.endpoint_id,
            headers=row.headers or {}, incarnation_id=row.incarnation_id,
            owner=row.owner,
        )

    def _db_row(self, session_id: str):
        db = core_database.SessionLocal()
        try:
            return db.query(DbSession).filter(DbSession.id == session_id).first()
        finally:
            db.close()

    def create_session(self, *, session_id: str, name: str, endpoint_url: str,
                       model: str, owner: str, endpoint_id=None, **_kwargs):
        from core.database import utcnow_naive

        db = core_database.SessionLocal()
        try:
            db.add(DbSession(
                id=session_id, name=name, endpoint_url=endpoint_url,
                model=model, owner=owner, endpoint_id=endpoint_id,
                revision=0, incarnation_id=uuid4().hex,
                created_at=utcnow_naive(), updated_at=utcnow_naive(),
            ))
            db.commit()
        finally:
            db.close()
        session = SimpleNamespace(
            id=session_id, name=name, endpoint_url=endpoint_url, model=model,
            owner=owner, endpoint_id=endpoint_id, headers={},
            incarnation_id=uuid4().hex, incognito=False,
            thinking_mode="off",
        )
        self.sessions[session_id] = session
        return session

    def update_session_name(self, session_id: str, name: str):
        db = core_database.SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is not None:
                row.name = name
                db.commit()
        finally:
            db.close()
        if session_id in self.sessions:
            self.sessions[session_id].name = name


class FakeComposer:
    """The composer facade contract with a synthetic completed stream."""

    def open_composed_text_events(self, admission, session, **_kwargs):
        outcome = TurnOutcome()

        async def frames():
            outcome.last_sequence += 1
            yield TurnEvent(outcome.last_sequence, "message.delta", {"text": "ok"})
            outcome.content = "ok"
            outcome.state = "completed"
            outcome.last_sequence += 1
            yield TurnEvent(outcome.last_sequence, "turn.completed", {})

        return TypedTurnDelivery(events=frames(), outcome=outcome)


class FakeApprovalStore:
    def __init__(self):
        self.pending: dict = {}

    def peek(self, approval_id: str):
        return self.pending.get(approval_id)


class FakePendingApproval:
    def __init__(self, approval_id: str, owner: str, session_id: str):
        self.approval_id = approval_id
        self.owner = owner
        self.session_id = session_id

    def public_payload(self):
        return {
            "kind": "tool_approval",
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "question": "Allow this task to continue?",
            "options": [{"label": "Allow", "value": "approve"}],
        }


@pytest.fixture()
def v1_app(tmp_path, monkeypatch):
    """The real router on a small app with a real temporary SQLite DB."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gateway-v1.sqlite'}",
        connect_args={"timeout": 15},
    )
    Base.metadata.create_all(bind=engine, tables=[
        DbSession.__table__, DbChatMessage.__table__,
        ModelEndpoint.__table__, GatewayTurn.__table__,
        GatewayTurnEvent.__table__, GatewayApproval.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(core_database, "SessionLocal", factory)

    app = FastAPI()
    app.state.session_manager = FakeManager()
    app.state.gateway_turns = FakeComposer()
    app.state.turn_service = TurnService()
    app.state.tool_approval_store = FakeApprovalStore()
    app.state.model_discovery = [{"id": "m1", "host": "example.com"}]
    app.state.tool_discovery = ["manage_notes"]
    app.state.gateway_turn_records = SqlTurnRecords(
        factory, GatewayTurn, GatewayTurnEvent, GatewayApproval,
    )
    app.include_router(gateway_router)
    app.state.test_engine = engine
    from src import llm_core

    # Default model registry for every create/update call in this module;
    # individual tests may narrow it with their own monkeypatch.
    monkeypatch.setattr(
        llm_core, "list_model_ids",
        lambda *_a, **_k: ["text-embedding-x", "vendor/m3-flash", "vendor/big"],
    )
    return app


@pytest.fixture()
def client(v1_app):
    return TestClient(v1_app)


def _auth(client):
    client.headers["Authorization"] = TOKEN


def _endpoint_row(owner=OWNER, endpoint_id="ep-1", base_url="http://127.0.0.1:9/v1"):
    return ModelEndpoint(
        id=endpoint_id, name="Test endpoint", base_url=base_url,
        api_key=None, is_enabled=True, owner=owner,
    )


def _seed_endpoint(engine, row):
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        db.add(row)
        db.commit()


def test_unauthenticated_requests_are_rejected(client):
    response = client.get("/api/v1/sessions")
    assert response.status_code == 401
    assert client.get("/api/v1/sessions/session-1").status_code == 401
    assert client.post(
        "/api/v1/sessions", json={"name": "x"},
    ).status_code == 401


def test_create_session_resolves_the_owner_endpoint_and_first_chat_model(
    v1_app, client, monkeypatch,
):
    _seed_endpoint(v1_app.state.test_engine, _endpoint_row())
    _auth(client)
    from src import llm_core

    monkeypatch.setattr(
        llm_core, "list_model_ids",
        lambda *_a, **_k: ["text-embedding-x", "vendor/m3-flash"],
    )
    response = client.post("/api/v1/sessions", json={"name": "CLI session"})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["id"] == data["session_id"]
    assert data["model"] == "vendor/m3-flash"
    assert data["mode"] == "chat"
    db = v1_app.state.test_engine
    with db.connect() as connection:
        row = connection.execute(
            DbSession.__table__.select().where(DbSession.name == "CLI session"),
        ).first()
        assert row is not None
        assert row.owner == OWNER
        assert row.model == "vendor/m3-flash"


def test_create_session_honors_an_explicit_endpoint_and_model_hint(
    v1_app, client, monkeypatch,
):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _seed_endpoint(engine, _endpoint_row(endpoint_id="ep-2", base_url="http://127.0.0.1:10/v1"))
    _auth(client)
    from src import llm_core

    monkeypatch.setattr(
        llm_core, "list_model_ids",
        lambda *_a, **_k: ["vendor/m3-flash", "vendor/big-model"],
    )
    response = client.post(
        "/api/v1/sessions",
        json={"name": "Pinned", "endpoint_id": "ep-2", "model": "m3-flash"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["model"] == "vendor/m3-flash"


def test_create_session_rejects_unknown_endpoints_and_models(
    v1_app, client, monkeypatch,
):
    _seed_endpoint(
        v1_app.state.test_engine, _endpoint_row(),
    )
    _auth(client)
    from src import llm_core

    monkeypatch.setattr(
        llm_core, "list_model_ids", lambda *_a, **_k: ["vendor/m3-flash"],
    )
    missing_endpoint = client.post(
        "/api/v1/sessions", json={"endpoint_id": "nope"},
    )
    assert missing_endpoint.status_code == 400
    missing_model = client.post(
        "/api/v1/sessions", json={"model": "not-offered"},
    )
    assert missing_model.status_code == 400
    foreign_endpoint = _endpoint_row(owner=OTHER, endpoint_id="ep-other")
    _seed_endpoint(
        v1_app.state.test_engine, foreign_endpoint,
    )
    assert client.post(
        "/api/v1/sessions", json={"endpoint_id": "ep-other"},
    ).status_code == 400


def test_create_session_without_registered_endpoints_fails_closed(client):
    _auth(client)
    response = client.post("/api/v1/sessions", json={"name": "orphan"})
    assert response.status_code == 400


def test_session_detail_returns_persisted_sanitized_history(v1_app, client):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    created = client.post("/api/v1/sessions", json={"name": "History"})
    session_id = created.json()["id"]
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        for index in range(3):
            db.add(DbChatMessage(
                id=f"m{index}", session_id=session_id, role="user" if index % 2 == 0 else "assistant",
                content=json.dumps([
                    {"type": "text", "text": f"mensagem {index}"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]) if index == 1 else f"mensagem {index}",
                meta_data=json.dumps({"hidden": True}) if index == 2 else None,
            ))
        db.commit()
    detail = client.get(f"/api/v1/sessions/{session_id}")
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["session"]["name"] == "History"
    assert data["total"] == 3
    # The hidden compaction row is excluded; the multimodal row drops media.
    assert [item["content"] for item in data["history"]] == [
        "mensagem 0", "mensagem 1",
    ]
    assert "data:image" not in data["history"][1]["content"]
    paged = client.get(
        f"/api/v1/sessions/{session_id}", params={"limit": 1, "offset": 1},
    )
    paged_data = paged.json()
    assert paged_data["history"] == [{"role": "assistant", "content": "mensagem 1", "metadata": {}}]
    assert paged_data["has_more_before"] is True
    assert paged_data["has_more_after"] is True


def test_foreign_sessions_stay_indistinguishable_from_missing(client, v1_app):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    created = client.post("/api/v1/sessions", json={"name": "mine"})
    session_id = created.json()["id"]
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        db.add(DbSession(
            id="foreign-1", name="not yours", endpoint_url="http://127.0.0.1:9/v1",
            model="m1", owner=OTHER, revision=0, incarnation_id="epoch-x",
        ))
        db.commit()
    assert client.get("/api/v1/sessions/foreign-1").status_code == 404
    assert client.get("/api/v1/sessions/missing").status_code == 404
    listed = client.get("/api/v1/sessions").json()["sessions"]
    assert [item["id"] for item in listed] == [session_id]


def test_session_approvals_listing_and_records_gate(v1_app, client):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    session_id = client.post("/api/v1/sessions", json={"name": "approvals"}).json()["id"]
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        db.add(GatewayApproval(
            approval_id="a" * 43, owner=OWNER, session_id=session_id,
            incarnation_id="epoch-a", turn_id="t1", tool_name="manage_notes",
            operation_digest="d" * 64, resource_identity=None,
            effective_authority=None, status="pending",
        ))
        db.commit()
    listing = client.get(f"/api/v1/sessions/{session_id}/approvals")
    assert listing.status_code == 200, listing.text
    items = listing.json()["approvals"]
    assert len(items) == 1
    assert items[0]["tool_name"] == "manage_notes"
    assert items[0]["status"] == "pending"
    # Display projection only: sealed authority material never leaves.
    assert "operation_digest" not in items[0]
    assert "effective_authority" not in items[0]
    foreign = client.get("/api/v1/sessions/foreign-1/approvals")
    assert foreign.status_code == 404
    # Without the durable record store the surface refuses loudly.
    records = v1_app.state.gateway_turn_records
    v1_app.state.gateway_turn_records = None
    assert client.get(f"/api/v1/sessions/{session_id}/approvals").status_code == 503
    v1_app.state.gateway_turn_records = records


def test_approval_preview_is_owner_checked_and_never_consumes(client, v1_app):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    session_id = client.post("/api/v1/sessions", json={"name": "preview"}).json()["id"]
    store = v1_app.state.tool_approval_store
    approval_id = "b" * 43
    store.pending[approval_id] = FakePendingApproval(approval_id, OWNER, session_id)
    preview = client.get(
        f"/api/v1/approvals/{approval_id}", params={"session_id": session_id},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["approval_id"] == approval_id
    foreign_owner = FakePendingApproval(approval_id, OTHER, session_id)
    store.pending[approval_id] = foreign_owner
    assert client.get(
        f"/api/v1/approvals/{approval_id}", params={"session_id": session_id},
    ).status_code == 404
    store.pending[approval_id] = FakePendingApproval(approval_id, OWNER, session_id)
    assert client.get(
        f"/api/v1/approvals/{approval_id}", params={"session_id": "missing"},
    ).status_code == 404
    assert approval_id in store.pending


def test_update_session_name_and_model_preference(v1_app, client, monkeypatch):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    from src import llm_core

    monkeypatch.setattr(
        llm_core, "list_model_ids", lambda *_a, **_k: ["vendor/m3-flash", "vendor/big"],
    )
    session_id = client.post("/api/v1/sessions", json={"name": "tune"}).json()["id"]
    renamed = client.patch(
        f"/api/v1/sessions/{session_id}", json={"name": "renamed"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "renamed"
    switched = client.patch(
        f"/api/v1/sessions/{session_id}", json={"model": "vendor/big"},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["model"] == "vendor/big"
    with engine.connect() as connection:
        row = connection.execute(
            DbSession.__table__.select().where(DbSession.id == session_id),
        ).first()
        assert row.name == "renamed"
        assert row.model == "vendor/big"
    rejected = client.patch(
        f"/api/v1/sessions/{session_id}", json={"model": "not-offered"},
    )
    assert rejected.status_code == 400
    assert client.patch("/api/v1/sessions/foreign-1", json={"name": "x"}).status_code == 404


def test_session_listing_prefers_the_database_and_merges_the_cache(
    v1_app, client,
):
    engine = v1_app.state.test_engine
    _seed_endpoint(engine, _endpoint_row())
    _auth(client)
    created_id = client.post("/api/v1/sessions", json={"name": "db row"}).json()["id"]
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        db.add(DbSession(
            id="old-1", name="Older conversation", endpoint_url="http://127.0.0.1:9/v1",
            model="m1", owner=OWNER, revision=0, incarnation_id="epoch-o",
        ))
        db.add(DbSession(
            id="ghost-1", name="Nobody", endpoint_url="http://127.0.0.1:9/v1",
            model="m1", owner=OWNER, revision=0, incarnation_id="epoch-g",
        ))
        db.commit()
    manager_session = SimpleNamespace(
        id="cached-1", owner=OWNER, name="Cached", model="m1",
    )
    v1_app.state.session_manager.sessions["cached-1"] = manager_session
    listed = client.get("/api/v1/sessions").json()["sessions"]
    ids = [item["id"] for item in listed]
    assert created_id in ids
    assert "old-1" in ids
    assert "cached-1" in ids
    # Hidden incognito-purge names stay invisible.
    assert "ghost-1" not in ids
    foreign = client.get("/api/v1/sessions").json()
    assert all(item["id"] != "foreign-1" for item in foreign["sessions"])
