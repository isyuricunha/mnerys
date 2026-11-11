"""Restart reconciliation changes audit state only, never executable grants."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from core.database import GatewayApproval, GatewayTurn, GatewayTurnEvent
from src.gateway_turn_records import SqlTurnRecords
from tests.test_mnerys_turn_records_integration import records_engine


def store(factory):
    return SqlTurnRecords(factory, GatewayTurn, GatewayTurnEvent, GatewayApproval)


def seed(records, *, status="running"):
    turn = uuid4().hex
    records.open_turn(owner="alice", session_id="restart-session", incarnation_id="epoch", turn_id=turn)
    records.append_event(turn_id=turn, owner="alice", sequence=1, kind="tool.started", payload={"tool": "bash"})
    if status != "running":
        records.finalize_turn(turn_id=turn, owner="alice", status=status)
    return turn


def test_restart_records_unknown_effects_once_without_replay(records_engine):
    factory, _ = records_engine
    records = store(factory)
    turn = seed(records)
    done = seed(records, status="completed")
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert records.interrupt_stale_turns(before=cutoff) >= 1
    assert records.read_turn(turn_id=turn, owner="alice")["status"] == "interrupted"
    events = list(records.iter_events(turn_id=turn, owner="alice"))
    assert [e["sequence"] for e in events] == [1, 2]
    assert events[-1]["kind"] == "turn.failed"
    assert events[-1]["payload"] == {"code": "interrupted", "effects": "unknown", "retryable": False}
    assert records.interrupt_stale_turns(before=cutoff) == 0
    assert list(records.iter_events(turn_id=turn, owner="alice")) == events
    assert records.read_turn(turn_id=done, owner="alice")["status"] == "completed"
    assert records.read_turn(turn_id=turn, owner="mallory") is None


def test_restart_retires_pending_approval_without_restoring_authority(records_engine):
    factory, _ = records_engine
    records = store(factory)
    turn = seed(records, status="awaiting_approval")
    approval = uuid4().hex
    records.record_approval(approval_id=approval, owner="alice", session_id="restart-session",
        incarnation_id="epoch", turn_id=turn, tool_name="bash", operation_digest="digest-only",
        resource_identity=None, effective_authority=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    records.interrupt_stale_turns(before=datetime.now(timezone.utc) + timedelta(seconds=1))
    projections = records.list_session_approvals(owner="alice", session_id="restart-session")
    result = next(item for item in projections if item["approval_id"] == approval)
    assert result["status"] == "superseded" and result["decision"] is None
    assert not records.decide_approval(approval_id=approval, owner="alice", decision="approve_task")
    assert not {"content", "authority", "operation_digest", "resource_identity"} & result.keys()
    assert records.read_turn(turn_id=turn, owner="alice")["status"] == "interrupted"


def test_boot_cutoff_preserves_current_process_and_atomic_terminal_is_owner_bound(records_engine):
    factory, _ = records_engine
    records = store(factory)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    turn = seed(records)
    records.interrupt_stale_turns(before=before)
    assert records.read_turn(turn_id=turn, owner="alice")["status"] == "running"
    assert not records.finalize_with_event(turn_id=turn, owner="mallory", status="cancelled",
        kind="turn.cancelled", payload={})
    assert records.finalize_with_event(turn_id=turn, owner="alice", status="cancelled",
        kind="turn.cancelled", payload={})
    assert not records.finalize_with_event(turn_id=turn, owner="alice", status="completed",
        kind="turn.completed", payload={})
    assert [e["kind"] for e in records.iter_events(turn_id=turn, owner="alice")] == ["tool.started", "turn.cancelled"]


def test_terminal_event_storage_failure_rolls_back_status(records_engine):
    from sqlalchemy import event
    factory, engine = records_engine
    records = store(factory)
    turn = seed(records)

    def reject_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO GATEWAY_TURN_EVENTS"):
            raise RuntimeError("isolated storage failure")

    event.listen(engine, "before_cursor_execute", reject_insert)
    try:
        with pytest.raises(RuntimeError, match="isolated storage failure"):
            records.finalize_with_event(turn_id=turn, owner="alice", status="completed",
                kind="turn.completed", payload={})
    finally:
        event.remove(engine, "before_cursor_execute", reject_insert)
    assert records.read_turn(turn_id=turn, owner="alice")["status"] == "running"
    assert [e["sequence"] for e in records.iter_events(turn_id=turn, owner="alice")] == [1]


def test_restart_expires_old_approval_and_preserves_consumed_decision(records_engine):
    factory, _ = records_engine
    records = store(factory)
    turn = seed(records)
    ids = [uuid4().hex, uuid4().hex]
    for approval in ids:
        records.record_approval(approval_id=approval, owner="alice", session_id="restart-expiry",
            incarnation_id="epoch", turn_id=turn, tool_name="bash", operation_digest="digest-only",
            resource_identity=None, effective_authority=None,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert records.decide_approval(approval_id=ids[1], owner="alice", decision="deny")
    records.interrupt_stale_turns(before=datetime.now(timezone.utc) + timedelta(seconds=1))
    results = {r["approval_id"]: r for r in records.list_session_approvals(owner="alice", session_id="restart-expiry")}
    assert results[ids[0]]["status"] == "expired"
    assert results[ids[1]]["status"] == "denied" and results[ids[1]]["decision"] == "deny"


def test_concurrent_terminal_writers_settle_exactly_once(records_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    factory, _ = records_engine
    records = store(factory)
    turn = seed(records)
    barrier = Barrier(2)

    def finish(status):
        barrier.wait(timeout=5)
        return records.finalize_with_event(turn_id=turn, owner="alice", status=status,
            kind="turn.completed" if status == "completed" else "turn.cancelled", payload={})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ("completed", "cancelled")))
    assert sorted(results) == [False, True]
    events = list(records.iter_events(turn_id=turn, owner="alice"))
    assert [e["sequence"] for e in events] == [1, 2]
    assert events[-1]["kind"] in {"turn.completed", "turn.cancelled"}


def test_recovered_status_and_events_are_visible_only_to_owner(records_engine, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import gateway_api
    from src.gateway_admission import Principal

    factory, _ = records_engine
    records = store(factory)
    turn = seed(records)
    records.interrupt_stale_turns(before=datetime.now(timezone.utc) + timedelta(seconds=1))
    app = FastAPI()
    app.state.gateway_turn_records = records
    app.include_router(gateway_api.router)
    monkeypatch.setattr(gateway_api, "principal_for_chat_request", lambda req:
        Principal("alice" if req.headers.get("authorization") == "Bearer owner" else "mallory", "browser"))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer owner"}
        snapshot = client.get(f"/api/v1/turns/{turn}", headers=headers)
        assert snapshot.status_code == 200 and snapshot.json()["status"] == "interrupted"
        events = client.get(f"/api/v1/turns/{turn}/events", headers=headers)
        assert events.status_code == 200 and '"code":"interrupted"' in events.text
        assert '"effects":"unknown"' in events.text
        assert client.get(f"/api/v1/turns/{turn}").status_code == 404
        assert client.get(f"/api/v1/turns/{turn}/events").status_code == 404


@pytest.mark.asyncio
async def test_web_disconnect_recovers_exact_run_and_durable_terminal(records_engine, monkeypatch):
    import asyncio
    from routes import chat_routes
    from src import agent_runs
    from tests.test_mnerys_gateway_web_core_cutover import chat_request, cutover_fixture

    records = store(records_engine[0])
    _, session, _, writes, routes = cutover_fixture(monkeypatch)
    session.incarnation_id = uuid4().hex
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"delta":"Recovered answer"}\n\n'
        await release.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", provider)
    request = chat_request({"message": "Say hello"})
    request.app.state.gateway_turn_records = records
    response = await routes["/api/chat_stream"](request)
    run_id = response.headers["X-Odysseus-Run-Id"]
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    request.headers["X-Odysseus-Run-Id"] = run_id
    resume = routes["/api/chat/resume/{session_id}"]
    resumed = await resume(request, session.id)
    release.set()
    assert "Recovered answer" in "".join([frame async for frame in resumed.body_iterator])
    assert calls == 1 and writes == ["Recovered answer"]
    assert records.read_turn(turn_id=run_id, owner="alice")["status"] == "completed"
    assert list(records.iter_events(turn_id=run_id, owner="alice")) == [
        {"sequence": 1, "kind": "turn.completed", "payload": {}},
    ]
    old = agent_runs._RUNS.pop(session.id)
    if old.evict_task:
        old.evict_task.cancel()
    recovered = await resume(request, session.id)
    assert recovered.headers["X-Mnerys-Recovery-State"] == "completed"
    assert [frame async for frame in recovered.body_iterator] == ["data: [DONE]\n\n"]
    assert calls == 1 and len(writes) == 1
    session.incarnation_id = uuid4().hex
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as rejected:
        await resume(request, session.id)
    assert rejected.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["deny", "restart"])
async def test_web_pending_approval_is_metadata_only_and_cannot_replay(records_engine, monkeypatch, decision):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from routes.history.history_routes import _project_history_approvals
    from src import agent_runs, turn_service as runtime
    from src.agent_tool_events import tool_event_frame
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action
    from tests.test_mnerys_gateway_web_core_cutover import chat_request, cutover_fixture

    records = store(records_engine[0])
    seals = ToolApprovalStore()
    admission, session, _, _, routes = cutover_fixture(monkeypatch, seals)
    session.incarnation_id = uuid4().hex
    monkeypatch.setattr("src.gateway_turns.tool_approval_store", seals)
    monkeypatch.setattr("src.tool_approvals.tool_approval_store", seals)
    pending = []
    command = "private-operation-not-for-audit"

    async def loop(*args, **kwargs):
        seal = seals.create(owner="alice", session_id=session.id, origin_run_id="test-origin",
            tool_name="bash", content=command, workspace=None, external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("bash", command))
        pending.append(seal)
        yield tool_event_frame({"type": "tool_output", "tool": "bash", "command": command,
            "ask_user": seal.public_payload(), "exit_code": None})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", loop)
    request = chat_request({"message": "List my notes", "mode": "agent"}, privileged=True)
    request.app.state.gateway_turn_records = records
    response = await routes["/api/chat_stream"](request)
    run_id = response.headers["X-Odysseus-Run-Id"]
    _ = [frame async for frame in response.body_iterator]
    seal = pending[0]
    assert records.read_turn(turn_id=run_id, owner="alice")["status"] == "awaiting_approval"
    events = list(records.iter_events(turn_id=run_id, owner="alice"))
    assert [e["kind"] for e in events] == ["approval.requested", "tool.awaiting_approval", "turn.awaiting_approval"]
    assert command not in str(events)
    card = {"role": "assistant", "metadata": {"tool_events": [{"ask_user": seal.public_payload()}]}}
    if decision == "deny":
        deny = chat_request({"message": "", "tool_approval_id": seal.approval_id, "tool_approval_decision": "deny"}, privileged=True)
        deny.app.state.gateway_turn_records = records
        denied = await routes["/api/chat_stream"](deny)
        assert '"decision": "deny"' in "".join([frame async for frame in denied.body_iterator])
        assert list(records.iter_events(turn_id=run_id, owner="alice"))[-1]["kind"] == "approval.decided"
        assert records.read_turn(turn_id=run_id, owner="alice")["status"] == "approval_decided"
    else:
        old = agent_runs._RUNS.pop(session.id)
        if old.evict_task:
            old.evict_task.cancel()
        records.interrupt_stale_turns(before=datetime.now(timezone.utc) + timedelta(seconds=1))
        empty = ToolApprovalStore()
        monkeypatch.setattr("src.gateway_turns.tool_approval_store", empty)
        monkeypatch.setattr("src.tool_approvals.tool_approval_store", empty)
        resumed = await routes["/api/chat/resume/{session_id}"](request, session.id)
        assert resumed.headers["X-Mnerys-Recovery-State"] == "interrupted"
    projected = _project_history_approvals([card], admission, session.id)
    assert projected[0]["metadata"]["tool_events"][0]["ask_user"] == {
        "kind": "tool_approval", "resolved": "unavailable",
    }
    replay = chat_request({"message": "", "tool_approval_id": seal.approval_id,
                           "tool_approval_decision": "approve_task"}, privileged=True)
    replay.app.state.gateway_turn_records = records
    with pytest.raises(HTTPException) as denied:
        await routes["/api/chat_stream"](replay)
    assert denied.value.status_code == 409
    assert len(pending) == 1
