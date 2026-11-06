"""Shared isolated app builder for M4 in-process router integration tests.

Builds a FastAPI app around the production Gateway v1 router with a real
temporary SQLite database, the real durable record store and a synthetic
composer stream. All router global seams (principal, model registry,
session factory) are patched through the caller's monkeypatch and therefore
restored after each test: this module performs NO import-time mutation, so
it can never pollute other suites.
"""

from types import SimpleNamespace

from fastapi import FastAPI
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
    utcnow_naive,
)
from routes import gateway_api
from routes.gateway_api import router as gateway_router
from src.gateway_admission import Principal
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedTurnDelivery
from src.gateway_turn_records import SqlTurnRecords
from src.turn_service import TurnService

OWNER = "alice"


class FakeComposer:
    """The composer facade contract with a synthetic completed stream."""

    def open_composed_text_events(self, admission, session, **_kwargs):
        outcome = TurnOutcome()
        request = _kwargs.get("request")
        message = request.message if request is not None else "echo"
        records = _kwargs.get("records")
        turn_id = _kwargs.get("turn_id")
        owner = _kwargs.get("context_owner")
        stuck = message == "wait-turn"

        async def frames():
            async def event(kind, payload):
                outcome.last_sequence += 1
                item = TurnEvent(outcome.last_sequence, kind, payload)
                if records is not None:
                    records.append_event(
                        turn_id=turn_id, owner=owner,
                        sequence=item.sequence, kind=item.type,
                        payload=item.payload,
                    )
                return item

            yield await event("message.delta", {"text": "partial" if stuck else message})
            if stuck:
                return  # this turn never terminates on its own; cancel ends it
            outcome.content = message
            outcome.state = "completed"
            yield await event("message.final", {"text": message})
            yield await event("turn.completed", {})
            if records is not None:
                records.finalize_turn(
                    turn_id=turn_id, owner=owner, status="completed",
                )

        return TypedTurnDelivery(events=frames(), outcome=outcome, turn_id=turn_id)


class FakeManager:
    """The manager surface the v1 routes need, backed by the real DB."""

    def __init__(self):
        self.sessions: dict = {}

    def get_session(self, session_id: str):
        db = core_database.SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is None:
                raise KeyError(session_id)
        finally:
            db.close()
        if session_id in self.sessions:
            return self.sessions[session_id]
        session = SimpleNamespace(
            id=row.id, name=row.name, model=row.model,
            endpoint_url=row.endpoint_url, owner=row.owner,
            endpoint_id=row.endpoint_id, headers=row.headers or {},
            incarnation_id=row.incarnation_id, incognito=False, mode="chat",
        )
        self.sessions[session_id] = session
        return session

    def create_session(self, *, session_id, name, endpoint_url, model, owner, endpoint_id=None, **_kwargs):
        db = core_database.SessionLocal()
        try:
            db.add(DbSession(
                id=session_id, name=name, endpoint_url=endpoint_url, model=model,
                owner=owner, endpoint_id=endpoint_id, revision=0,
                incarnation_id="epoch-a", created_at=utcnow_naive(), updated_at=utcnow_naive(),
            ))
            db.commit()
        finally:
            db.close()
        session = SimpleNamespace(
            id=session_id, name=name, endpoint_url=endpoint_url, model=model,
            owner=owner, endpoint_id=endpoint_id, headers={},
            incarnation_id="epoch-a", incognito=False, mode="chat",
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


def build_v1_app(monkeypatch, tmp_path, *, seed_session_id: str | None = None):
    """One isolated app with the production router, DB and durable records."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'v1-router.sqlite'}",
        connect_args={"timeout": 15},
    )
    Base.metadata.create_all(bind=engine, tables=[
        DbSession.__table__, DbChatMessage.__table__, ModelEndpoint.__table__,
        GatewayTurn.__table__, GatewayTurnEvent.__table__, GatewayApproval.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(core_database, "SessionLocal", factory)

    def _principal(request):
        if request.headers.get("authorization") == "Bearer tok":
            return Principal(OWNER, "bearer", frozenset({"chat"}))
        return Principal(None, "browser", frozenset())

    monkeypatch.setattr(gateway_api, "principal_for_chat_request", _principal)
    from src import llm_core

    monkeypatch.setattr(
        llm_core, "list_model_ids",
        lambda *_a, **_k: ["m1", "vendor/big"],
    )

    app = FastAPI()
    app.state.session_manager = FakeManager()
    app.state.gateway_turns = FakeComposer()
    app.state.turn_service = TurnService()
    app.state.chat_handler = SimpleNamespace()
    app.state.chat_processor = SimpleNamespace()
    app.state.chat_context_ports = SimpleNamespace()
    app.state.tool_approval_store = SimpleNamespace(peek=lambda _id: None)
    app.state.model_discovery = [{"id": "m1"}]
    app.state.tool_discovery = ["manage_notes"]
    app.state.gateway_turn_records = SqlTurnRecords(
        factory, GatewayTurn, GatewayTurnEvent, GatewayApproval,
    )
    app.include_router(gateway_router)
    app.state.test_engine = engine

    if seed_session_id:
        with factory() as db:
            db.add(DbSession(
                id=seed_session_id, name="Web conversation",
                endpoint_url="http://127.0.0.1:9/v1", model="m1", owner=OWNER,
                revision=0, incarnation_id="epoch-a",
                created_at=utcnow_naive(), updated_at=utcnow_naive(),
            ))
            db.add(DbChatMessage(
                id="w1", session_id=seed_session_id, role="user",
                content="hello from the web", timestamp=utcnow_naive(),
            ))
            db.add(DbChatMessage(
                id="w2", session_id=seed_session_id, role="assistant",
                content="web reply", timestamp=utcnow_naive(),
            ))
            db.commit()
    return app