"""Loopback acceptance server for the installed mnerys CLI (test-only).

Mounts the real Gateway v1 router with a synthetic bearer principal and a
fake composed stream, so the acceptance tests exercise genuine HTTP and
the real detached run manager without a provider or a database. Binds
loopback only; total test surface.
"""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI

from routes import gateway_api
from routes.gateway_api import router as gateway_router
from src.gateway_admission import Principal
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedTurnDelivery
from src.turn_service import TurnService


def _principal(request):
    if request.headers.get("authorization") == "Bearer test-token":
        return Principal("alice", "bearer", frozenset({"chat"}))
    return Principal(None, "browser", frozenset())


gateway_api.principal_for_chat_request = _principal


class FakeComposer:
    """The composer facade contract with a synthetic completed stream."""

    def open_composed_text_events(
        self, admission, session, *, context_owner, request, ports,
        chat_handler, chat_processor, settings, client_rendering,
        records=None, turn_id=None,
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


app = FastAPI()
app.state.session_manager = SimpleNamespace(sessions={
    "session-1": SimpleNamespace(
        id="session-1", owner="alice", name="Acceptance", model="m1",
        incognito=False, incarnation_id="epoch-a",
    ),
})
app.state.gateway_turns = FakeComposer()
app.state.turn_service = TurnService()
app.state.chat_handler = SimpleNamespace()
app.state.chat_processor = SimpleNamespace()
app.state.chat_context_ports = SimpleNamespace()
app.state.tool_approval_store = StubStore()
app.state.model_discovery = [{"id": "m1", "host": "example.com"}]
app.state.tool_discovery = ["manage_notes"]
app.include_router(gateway_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/v1/sessions")
async def create_session():
    session_id = "cli-new"
    app.state.session_manager.sessions[session_id] = SimpleNamespace(
        id=session_id, owner="alice", name="CLI", model="m1",
        incognito=False, incarnation_id="epoch-a",
    )
    return {"id": session_id, "session_id": session_id}