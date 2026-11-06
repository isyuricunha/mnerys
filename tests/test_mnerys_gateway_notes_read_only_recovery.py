"""Post-restart metadata readout never reconstitutes or authorizes a tool."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import agent_runs, turn_service as runtime
from src.gateway_admission import GatewayAdmission, Principal
from src.session_repository import SessionOwnership
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs
from tests.test_mnerys_gateway_web_native_notes_integration import (
    route_fixture, request,
)
from tests.test_mnerys_gateway_web_notes_ui_recovery import open_pending_note
import routes.chat_routes as routes


@pytest.mark.asyncio
async def test_restarted_web_status_returns_non_actionable_owner_bound_classification(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    fence = routes.server_notes_decision_fence()
    created = []
    await open_pending_note(monkeypatch, created, session, store)
    original_run = None
    try:
        response = await handlers["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        first = [event async for event in response.body_iterator]
        assert any('"type":"ask_user"' in event for event in first)
        assert len(created) == 1
        original_run = agent_runs._RUNS.pop(session.id)
        assert agent_runs.get_recoverable_notes_review(session.id) is None

        recovery = await handlers["/api/chat/stream_status/{session_id}"](
            request({}), session.id,
        )
        assert recovery == {
            "status": "review_unavailable",
            "gateway_profile": "notes_v1",
            "recovery": "interrupted",
            "detached": False,
        }
        assert created[0].approval_id not in repr(recovery)
        assert created[0].content not in repr(recovery)
        assert not writes and not partials
        assert ports.operations.count("save_user") == 1

        user_receipt = session.history[-1].metadata["_db_id"]
        assert fence.reserve(
            owner="alice", session_id=session.id,
            approval_id=created[0].approval_id,
            action_digest=created[0].digest,
            origin_user_receipt=user_receipt,
            decision="approve_task",
        )
        uncertain = await handlers["/api/chat/stream_status/{session_id}"](
            request({}), session.id,
        )
        assert uncertain["recovery"] == "indeterminate"
        with pytest.raises(HTTPException) as no_replay:
            await handlers["/api/chat/resume/{session_id}"](
                request({}), session.id,
            )
        assert no_replay.value.status_code == 404
        assert store.peek(created[0].approval_id) is created[0]

        # A successful later message changes the transcript receipt and
        # prevents the old review from reappearing on session reentry.
        session.history.append(SimpleNamespace(
            role="assistant", metadata={"_db_id": "later-assistant"},
        ))
        with pytest.raises(HTTPException) as stale:
            await handlers["/api/chat/stream_status/{session_id}"](
                request({}), session.id,
            )
        assert stale.value.status_code == 404
        session.history.pop()

        # A delegated integration cannot infer even the read-only state.
        bearer = GatewayAdmission().admit(
            Principal("alice", "bearer", frozenset({"chat"})), session.id,
            lambda sid: SessionOwnership(owner_id="alice"),
        )
        monkeypatch.setattr(
            routes, "_verify_session_owner", lambda req, sid: bearer,
        )
        with pytest.raises(HTTPException) as delegated:
            await handlers["/api/chat/stream_status/{session_id}"](
                request({}), session.id,
            )
        assert delegated.value.status_code == 404
    finally:
        if original_run is not None:
            agent_runs._RUNS[session.id] = original_run
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_no_restart_status_without_original_receipt_or_opt_in(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    session.history.append(SimpleNamespace(
        role="user", metadata={},
    ))
    with pytest.raises(HTTPException) as no_receipt:
        await handlers["/api/chat/stream_status/{session_id}"](
            request({}), session.id,
        )
    assert no_receipt.value.status_code == 404

    monkeypatch.setenv("MNERYS_M21_WEB_NATIVE_NOTES_ROUTING", "0")
    session.history[-1].metadata["_db_id"] = "arbitrary-user"
    with pytest.raises(HTTPException) as off:
        await handlers["/api/chat/stream_status/{session_id}"](
            request({}), session.id,
        )
    assert off.value.status_code == 404
    assert not writes and not partials and not ports.operations
