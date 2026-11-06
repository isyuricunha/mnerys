"""M2.4 Gateway API v1 acceptance: router behavior and negative security.

The router is mounted on a standalone FastAPI app with mocks for the
inherited services; the independent remote client exercises the same app
over HTTP without importing the router, the database or the Agent Loop.
"""

import re
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.gateway_api import router as gateway_router
from src.gateway_admission import Principal
from src.gateway_turn_events import TurnOutcome
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured

CLIENT = 0
TURN_ID_RE = re.compile(r"[0-9a-f]{32}")


def _fake_session(session_id="session-1", owner="alice"):
    from core.models import Session

    return Session(
        id=session_id, owner=owner, name="API", model="fixture-model",
        endpoint_url="https://example.com/v1", headers={},
        revision=0, incarnation_id="epoch-a",
    )


class FakeComposerGateway:
    """The exact Gateway facade contract the app wires, minus the loop."""

    def open_composed_text_events(self, admission, session, *, context_owner,
                                  request, ports, chat_handler, chat_processor,
                                  settings, client_rendering, records=None,
                                  turn_id=None, persistence=None):
        outcome = TurnOutcome()

        async def frames():
            async def event(kind, payload):
                outcome.last_sequence += 1
                from src.gateway_turn_events import TurnEvent

                item = TurnEvent(outcome.last_sequence, kind, payload)
                if records is not None:
                    records.append_event(
                        turn_id=turn_id, owner=context_owner,
                        sequence=item.sequence, kind=item.type,
                        payload=item.payload,
                    )
                return item

            yield await event("message.delta", {"text": "Remote answer."})
            outcome.content = "Remote answer."
            outcome.state = "completed"
            yield await event("message.final", {"text": "Remote answer."})
            yield await event("turn.completed", {})
            if records is not None:
                records.finalize_turn(
                    turn_id=turn_id, owner=context_owner, status="completed",
                )

        return __import__(
            "src.gateway_turn_events", fromlist=["TypedTurnDelivery"],
        ).TypedTurnDelivery(events=frames(), outcome=outcome, turn_id=turn_id)

    def decide_tool_approval(self, admission, approval_id, decision, *,
                             plan_mode, internal_tool_request, store,
                             records=None, session=None):
        from src.gateway_approval_decisions import (
            consume_admitted_tool_approval,
        )

        return consume_admitted_tool_approval(
            admission, approval_id, decision, plan_mode=plan_mode,
            internal_tool_request=internal_tool_request, store=store,
            records=records, session=session,
        )


class DummyTurnService(TurnService):
    """The real detached runner fed by the composed fake stream."""

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch

    def start_detached_typed(self, session_id, events, outcome, *,
                             web_strict_text=False, web_native_notes=False,
                             run_id=None):
        return super().start_detached_typed(
            session_id, events, outcome,
            web_strict_text=web_strict_text, web_native_notes=web_native_notes,
            run_id=run_id,
        )


def _make_app(monkeypatch, *, with_records=False, tmp_path=None):
    admission, _session, ports, options = configured(monkeypatch)
    owner = "alice"
    session = _fake_session()
    manager = SimpleNamespace(sessions={"session-1": session})
    store = ToolApprovalStore()
    gateway = FakeComposerGateway()
    app = FastAPI()
    app.state.session_manager = manager
    app.state.gateway_turns = gateway
    app.state.turn_service = DummyTurnService(monkeypatch)
    app.state.chat_handler = options["chat_handler"]
    app.state.chat_processor = options["chat_processor"]
    app.state.chat_context_ports = ports
    app.state.tool_approval_store = store
    app.state.model_discovery = [{"id": "fixture-model", "host": "example.com"}]
    app.state.tool_discovery = ["manage_notes"]
    if with_records:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import (
            Base, GatewayApproval, GatewayTurn, GatewayTurnEvent,
        )
        from src.gateway_turn_records import SqlTurnRecords

        engine = create_engine(
            f"sqlite:///{tmp_path / 'records.sqlite'}"
            if tmp_path is not None else "sqlite:///:memory:",
        )
        Base.metadata.create_all(engine, tables=[
            GatewayTurn.__table__, GatewayTurnEvent.__table__,
            GatewayApproval.__table__,
        ])
        app.state.gateway_turn_records = SqlTurnRecords(
            sessionmaker(bind=engine), GatewayTurn, GatewayTurnEvent,
            GatewayApproval,
        )
    app.include_router(gateway_router)

    def fake_principal(request):
        auth = request.headers.get("authorization")
        if auth == "Bearer test-token":
            return Principal("alice", "bearer", frozenset({"chat"}))
        if auth == "Bearer browser-token":
            return Principal("alice", "browser", frozenset())
        return Principal(None, "browser", frozenset())

    monkeypatch.setattr(
        "routes.gateway_api.principal_for_chat_request", fake_principal,
    )
    return TestClient(app), session, admission


def _headers():
    return {"Authorization": "Bearer test-token"}


def _create_turn(client, **overrides):
    body = {"session_id": "session-1", "message": "Run it"}
    body.update(overrides)
    return client.post("/api/v1/turns", json=body, headers=_headers())


def test_turns_create_streams_and_cancels(monkeypatch):
    client, _session, _admission = _make_app(monkeypatch)
    created = _create_turn(client, mode="agent")
    assert created.status_code == 201, created.text
    turn_id = created.json()["turn_id"]
    assert TURN_ID_RE.fullmatch(turn_id)
    snapshot = client.get(f"/api/v1/turns/{turn_id}", headers=_headers())
    assert snapshot.status_code == 200, snapshot.text
    events = client.get(f"/api/v1/turns/{turn_id}/events", headers=_headers())
    assert events.status_code == 200
    assert "Remote answer." in events.text
    assert "data: [DONE]" in events.text
    cancel = client.post(
        f"/api/v1/turns/{turn_id}/cancel",
        json={"session_id": "session-1"}, headers=_headers(),
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"


def test_turns_require_an_authenticated_owner(monkeypatch):
    client, _session, _admission = _make_app(monkeypatch)
    anonymous = client.post(
        "/api/v1/turns",
        json={"session_id": "session-1", "message": "Run it"},
    )
    assert anonymous.status_code == 401
    wrong_scope = client.get(
        "/api/v1/sessions",
        headers={"Authorization": "Bearer no-chat-scope"},
    )
    assert wrong_scope.status_code == 401


def test_sessions_and_discovery_are_metadata_only(monkeypatch):
    client, _session, _admission = _make_app(monkeypatch)
    listing = client.get("/api/v1/sessions", headers=_headers())
    assert [item["id"] for item in listing.json()["sessions"]] == ["session-1"]
    models = client.get("/api/v1/models", headers=_headers())
    assert models.json()["models"][0]["id"] == "fixture-model"
    tools = client.get("/api/v1/tools", headers=_headers())
    assert "manage_notes" in tools.json()["tools"]
    assert "test-token" not in models.text


def test_durable_records_back_snapshots_replay_and_idempotency(monkeypatch, tmp_path):
    client, _session, _admission = _make_app(
        monkeypatch, with_records=True, tmp_path=tmp_path,
    )
    created = _create_turn(client, mode="agent", idempotency_key="key-1")
    assert created.status_code == 201, created.text
    turn_id = created.json()["turn_id"]
    snapshot = client.get(f"/api/v1/turns/{turn_id}", headers=_headers())
    assert snapshot.json()["status"] == "completed"
    events = client.get(
        f"/api/v1/turns/{turn_id}/events?after_seq=0", headers=_headers(),
    )
    assert "message.delta" in events.text
    replay = client.get(
        f"/api/v1/turns/{turn_id}/events?after_seq=2", headers=_headers(),
    )
    assert '"sequence":3' in replay.text
    conflict = client.post(
        "/api/v1/turns",
        json={
            "session_id": "session-1", "message": "Duplicate",
            "idempotency_key": "key-1",
        },
        headers=_headers(),
    )
    assert conflict.status_code == 409
    # A foreign owner cannot read the same turn.
    monkeypatch.setattr(
        "routes.gateway_api.principal_for_chat_request",
        lambda request: Principal("bob", "bearer", frozenset({"chat"})),
    )
    assert client.get(f"/api/v1/turns/{turn_id}", headers=_headers()).status_code == 404


def test_remote_client_cannot_decide_approvals_by_design(monkeypatch, tmp_path):
    """Delegated tokens never decide approvals; the owner's browser does."""
    from src.agent_runtime.authority import RequestAuthority
    from src.tool_capabilities import capabilities_for_action
    from tests.remote_gateway.client import RemoteGatewayClient

    client_app, _session, _admission = _make_app(
        monkeypatch, with_records=True, tmp_path=tmp_path,
    )
    store = client_app.app.state.tool_approval_store
    pending = store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1",
        tool_name="manage_notes", content='{"action":"list"}',
        workspace=None, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("manage_notes", '{"action":"list"}'),
        request_authority=RequestAuthority.empty(owner="alice", session_id="session-1"),
    )
    remote = RemoteGatewayClient(client=client_app, token="test-token")
    try:
        # A delegated bearer must be refused before it can consume anything.
        from tests.remote_gateway.client import RemoteGatewayError

        refused = None
        try:
            remote.decide_approval(pending.approval_id, "session-1", "approve")
        except RemoteGatewayError as exc:
            refused = exc
        assert refused is not None and refused.status_code == 403
        # The owner's browser decides against the same shared store, exactly
        # once, and the durable record is sealed with its bound identity.
        decided = client_app.post(
            "/api/v1/approvals/" + pending.approval_id + "/decision",
            params={"session_id": "session-1"},
            json={"decision": "approve"},
            headers={"Authorization": "Bearer browser-token"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["decision"] == "approve"
        replay = client_app.post(
            "/api/v1/approvals/" + pending.approval_id + "/decision",
            params={"session_id": "session-1"},
            json={"decision": "deny"},
            headers={"Authorization": "Bearer browser-token"},
        )
        assert replay.status_code == 409
        records = client_app.app.state.gateway_turn_records
        with records.session_factory() as db:
            from core.database import GatewayApproval

            row = db.get(GatewayApproval, pending.approval_id)
            assert row is not None
            assert row.status == "consumed"
            assert row.owner == "alice"
            assert row.operation_digest == pending.digest
    finally:
        remote.close()


def test_remote_turn_continues_the_session_when_persistence_is_wired(monkeypatch, tmp_path):
    """The remote turn persists through the same writer wrapper as Web turns."""
    from src.gateway_text_persistence import TrustedTextPersistence
    from tests.remote_gateway.client import RemoteGatewayClient

    client_app, _session, _admission = _make_app(
        monkeypatch, with_records=True, tmp_path=tmp_path,
    )
    saved = []

    def saver(current, manager, session_id, text, metrics, **kwargs):
        saved.append(text)
        from core.models import ChatMessage

        current.history.append(ChatMessage(
            "assistant", text,
            metadata={"_db_id": f"fixture-assistant-{len(current.history) + 1}"},
        ))
        return "remote-saved-answer"

    client_app.app.state.text_persistence = TrustedTextPersistence(
        session_manager=SimpleNamespace(), saver=saver,
        emit_message_saved_events=True,
    )
    remote = RemoteGatewayClient(client=client_app, token="test-token")
    try:
        created = remote.create_turn("session-1", "Continuing state")
        snapshot = remote.turn(created.turn_id)
        assert snapshot.status in ("completed", "running")
    finally:
        remote.close()
    assert saved == ["Remote answer."]


def test_direct_loop_bypass_is_not_possible():
    """The router surface must not import or invoke the Agent Loop."""
    import inspect

    import routes.gateway_api as api

    source = inspect.getsource(api)
    assert "stream_agent_loop" not in source
    assert "stream_llm" not in source
    assert "SessionLocal" not in source


def test_remote_client_runs_the_same_turn_contract(monkeypatch, tmp_path):
    """One authoritative state and policy, exercised over pure HTTP."""
    from tests.remote_gateway.client import (
        RemoteGatewayClient, RemoteGatewayError,
    )

    client_app, _session, _admission = _make_app(
        monkeypatch, with_records=True, tmp_path=tmp_path,
    )
    remote = RemoteGatewayClient(client=client_app, token="test-token")
    try:
        assert remote.sessions() == [{
            "id": "session-1", "name": "API", "model": "fixture-model",
            "mode": "chat",
        }]
        models = remote.models()
        assert models[0]["id"] == "fixture-model"
        assert "manage_notes" in remote.tools()
        created = remote.create_turn(
            "session-1", "Remote parity", mode="agent",
            idempotency_key="parity-key",
        )
        assert created.status == "running"
        snapshot = remote.turn(created.turn_id)
        assert snapshot.status in ("completed", "running")
        items = remote.events(created.turn_id)
        kinds = [item["kind"] for item in items]
        assert "message.delta" in kinds
        assert items[-1]["kind"] == "turn.completed"
        # Replay from a cursor resumes after the gap.
        after = remote.events(created.turn_id, after_seq=items[0]["sequence"])
        assert all(item["sequence"] > items[0]["sequence"] for item in after)
        # Duplicate idempotency key with a different turn is a conflict.
        duplicate = None
        try:
            remote.create_turn(
                "session-1", "Duplicate", idempotency_key="parity-key",
            )
        except RemoteGatewayError as exc:
            duplicate = exc
        assert duplicate is not None and duplicate.status_code == 409
        # The remote client can cancel the exact turn.
        cancelled = remote.cancel(created.turn_id, "session-1")
        assert cancelled["status"] == "cancelled"
    finally:
        remote.close()


def test_remote_client_never_imports_the_server(monkeypatch, tmp_path):
    """The scripted client is transport-only: no app, DB or loop imports."""
    import inspect
    import sys

    from tests.remote_gateway.client import RemoteGatewayClient

    source = inspect.getsource(sys.modules[RemoteGatewayClient.__module__])
    assert "routes" not in source
    assert "core.database" not in source
    assert "src." not in source
    assert "stream_agent_loop" not in source
    assert "SessionLocal" not in source


def test_web_and_remote_share_one_snapshot_state(monkeypatch, tmp_path):
    """The Web-facing registry and the remote API observe the same turn."""
    from tests.remote_gateway.client import RemoteGatewayClient

    client_app, _session, _admission = _make_app(
        monkeypatch, with_records=True, tmp_path=tmp_path,
    )
    remote = RemoteGatewayClient(client=client_app, token="test-token")
    try:
        created = remote.create_turn("session-1", "Shared state")
        # The in-process registry sees the exact same turn id.
        from src import agent_runs

        run = agent_runs.get_matching_run(
            "session-1", created.turn_id, mode="typed",
        )
        assert run is not None
        assert run.status in ("running", "done")
        # The durable record is the same single authoritative record.
        records = client_app.app.state.gateway_turn_records
        turn = records.read_turn(turn_id=created.turn_id, owner="alice")
        assert turn is not None
        assert turn["status"] in ("completed", "running", "cancelled")
    finally:
        remote.close()