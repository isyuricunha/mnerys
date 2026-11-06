"""Loopback acceptance server for the installed mnerys CLI (test-only).

Mounts the real Gateway v1 router with a synthetic bearer principal, a fake
composed stream, a real temporary SQLite database and the real durable
record store, so the acceptance tests exercise genuine HTTP, the real
detached run manager and the owner-scoped session surfaces without a
provider. Binds loopback only; total test surface.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import database as core_database
from core.database import (
    Base,
    ChatMessage as DbChatMessage,
    GatewayApproval,
    GatewayTurn,
    GatewayTurnEvent,
    ModelEndpoint,
    ScheduledTask,
    Session as DbSession,
)
from routes import gateway_api
from routes.gateway_api import router as gateway_router
from src import llm_core
from src.gateway_admission import Principal
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedTurnDelivery
from src.turn_service import TurnService


def _principal(request):
    if request.headers.get("authorization") == "Bearer test-token":
        return Principal("alice", "bearer", frozenset({"chat"}))
    return Principal(None, "browser", frozenset())


gateway_api.principal_for_chat_request = _principal

# The fixture has no live model provider: answer the registry probe with a
# stable model list, exactly one chat model plus noise, for every endpoint.
llm_core.list_model_ids = lambda *_a, **_k: [
    "text-embedding-noise", "vendor/m3-flash",
]


class FixtureManager:
    """The manager surface the v1 routes need, backed by the real DB."""

    def __init__(self, factory):
        self.factory = factory
        self.sessions: dict = {}
        import os

        if os.environ.get("FIXTURE_SEED_SESSION", "1") != "0":
            self.sessions["session-1"] = SimpleNamespace(
                id="session-1", owner="alice", name="Acceptance", model="m1",
                incognito=False, incarnation_id="epoch-a",
            )

    def get_session(self, session_id: str):
        if session_id in self.sessions:
            return self.sessions[session_id]
        with self.factory() as db:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is None:
                raise KeyError(session_id)
            return SimpleNamespace(
                id=row.id, name=row.name, model=row.model,
                endpoint_url=row.endpoint_url, endpoint_id=row.endpoint_id,
                headers=row.headers or {}, incarnation_id=row.incarnation_id,
                owner=row.owner, incognito=False,
            )

    def create_session(self, *, session_id: str, name: str, endpoint_url: str,
                       model: str, owner: str, endpoint_id=None, **_kwargs):
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            db.add(DbSession(
                id=session_id, name=name, endpoint_url=endpoint_url,
                model=model, owner=owner, endpoint_id=endpoint_id,
                revision=0, incarnation_id=uuid.uuid4().hex,
                created_at=now, updated_at=now,
            ))
            db.commit()
        session = SimpleNamespace(
            id=session_id, name=name, endpoint_url=endpoint_url, model=model,
            owner=owner, endpoint_id=endpoint_id, headers={},
            incarnation_id=uuid.uuid4().hex, incognito=False,
            thinking_mode="off",
        )
        self.sessions[session_id] = session
        return session

    def update_session_name(self, session_id: str, name: str):
        with self.factory() as db:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is not None:
                row.name = name
                db.commit()
        if session_id in self.sessions:
            self.sessions[session_id].name = name


class FakeComposer:
    """The composer facade contract with a synthetic completed stream."""

    def open_composed_text_events(
        self, admission, session, *, context_owner, request, ports,
        chat_handler, chat_processor, settings, client_rendering,
        records=None, turn_id=None, persistence=None,
    ):
        outcome = TurnOutcome()
        message = request.message
        stuck = message == "wait-turn"
        deltas = [{"text": "Olá, CLI!"}]
        if stuck:
            deltas = [{"text": "partial"}]

        async def frames():
            async def event(kind, payload):
                outcome.last_sequence += 1
                return TurnEvent(outcome.last_sequence, kind, payload)

            for delta in deltas:
                yield await event("message.delta", delta)
                if stuck:
                    break
            if not stuck:
                outcome.content = deltas[0]["text"]
                outcome.state = "completed"
                yield await event("message.final", deltas[0])
                yield await event("turn.completed", {})

        return TypedTurnDelivery(events=frames(), outcome=outcome, turn_id=turn_id)


class StubStore:
    def peek(self, _approval_id):
        return None

    def retire_for_session(self, **kwargs):
        return False


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"timeout": 15},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=engine, tables=[
    DbSession.__table__, DbChatMessage.__table__, ModelEndpoint.__table__,
    GatewayTurn.__table__, GatewayTurnEvent.__table__, GatewayApproval.__table__,
    ScheduledTask.__table__,
])
factory = sessionmaker(bind=engine, expire_on_commit=False)
# The v1 routes open their own sessions through core.database.SessionLocal;
# point that binding at the fixture engine so the real router reads and
# writes the same isolated database (the lazy in-route imports resolve this
# attribute at call time).
core_database.SessionLocal = factory
with factory() as db:
    db.add(ModelEndpoint(
        id="ep-fixture", name="Fixture endpoint",
        base_url="http://127.0.0.1:9/v1", api_key=None,
        is_enabled=True, owner="alice",
    ))
    db.commit()

app = FastAPI()
app.state.session_manager = FixtureManager(factory)
app.state.gateway_turns = FakeComposer()
app.state.turn_service = TurnService()
app.state.chat_handler = SimpleNamespace()
app.state.chat_processor = SimpleNamespace()
app.state.chat_context_ports = SimpleNamespace()
app.state.tool_approval_store = StubStore()
app.state.model_discovery = [{"id": "m1", "host": "example.com"}]
app.state.tool_discovery = ["manage_notes"]
# The live fixture keeps the M3 flow: the detached run manager replays the
# typed events from its own buffer, exactly like the M3 acceptance. The
# durable record-backed endpoints (approvals listing) are covered by the
# real-SQLite integration suite instead of this loopback server.
app.include_router(gateway_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
