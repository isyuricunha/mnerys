"""Owner-bound browser recovery of a process-local pending notes approval.

The original Web route, signed pending store and detached run remain the only
sources of review data. No real provider, browser session or production DB.
"""

import pytest
from fastapi import HTTPException

import routes.chat_routes as routes
from src import agent_runs, turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_admission import GatewayAdmission, Principal
from src.session_repository import SessionOwnership
from src.tool_capabilities import capabilities_for_action
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs
from tests.test_mnerys_gateway_web_native_notes_integration import (
    request, route_fixture,
)


async def open_pending_note(monkeypatch, record, session, store):
    async def model(_url, _model, _messages, **kwargs):
        content = '{"action":"list"}'
        approval = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="notes-web-reentry-test",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        record.append(approval)
        yield approval_requested_event(approval)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE_NOTES",
        }, result={"ask_user": approval.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(runtime, "stream_agent_loop", model)


@pytest.mark.asyncio
async def test_pending_notes_status_and_no_id_resume_restore_one_sealed_card(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    pending = []
    await open_pending_note(monkeypatch, pending, session, store)
    try:
        response = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        first_frames = [frame async for frame in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        assert response.headers["x-mnerys-gateway-profile"] == "notes_v1"
        assert len(pending) == 1
        assert 'ask_user' in "".join(first_frames)
        assert ports.operations.count("save_user") == 1
        assert not writes and not partials
        assert session.notes_route_post_jobs == []
        run = agent_runs.get_recoverable_notes_review(session.id)
        assert run is not None and run.run_id == run_id

        status = await endpoints["/api/chat/stream_status/{session_id}"](
            request({}), session.id,
        )
        assert status == {
            "status": "awaiting_approval",
            "gateway_profile": "notes_v1",
            "detached": True,
        }
        restored = await endpoints["/api/chat/resume/{session_id}"](
            request({}), session.id,
        )
        frames = [frame async for frame in restored.body_iterator]
        assert restored.headers["x-odysseus-run-id"] == run_id
        assert restored.headers["x-mnerys-gateway-profile"] == "notes_v1"
        assert len([f for f in frames if '"type":"ask_user"' in f]) == 1
        assert frames[-1] == "data: [DONE]\n\n"
        assert "PRIVATE_NOTES" not in repr(frames)
        assert store.peek(pending[0].approval_id) is pending[0]
        assert ports.operations.count("save_user") == 1
        assert len(session.history) == 1
        assert not writes and not session.notes_route_post_jobs

        # Explicit Last-Event-ID suppresses an already acknowledged card.
        review_cursor = next(
            line[4:] for f in frames if '"type":"ask_user"' in f
            for line in f.splitlines() if line.startswith("id: ")
        )
        not_repeated = await endpoints["/api/chat/resume/{session_id}"](
            request({}, run_id=run_id, cursor=review_cursor), session.id,
        )
        assert [frame async for frame in not_repeated.body_iterator] == []
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_browser_recovery_does_not_discover_card_with_delegated_credentials(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    pending = []
    await open_pending_note(monkeypatch, pending, session, store)
    try:
        first = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        _ = [frame async for frame in first.body_iterator]
        bearer = GatewayAdmission().admit(
            Principal("alice", "bearer", frozenset({"chat"})), session.id,
            lambda sid: SessionOwnership(owner_id="alice"),
        )
        monkeypatch.setattr(routes, "_verify_session_owner", lambda req, sid: bearer)
        for handler in (
            endpoints["/api/chat/stream_status/{session_id}"],
            endpoints["/api/chat/resume/{session_id}"],
        ):
            with pytest.raises(HTTPException) as error:
                await handler(request({}), session.id)
            assert error.value.status_code == 404
        assert store.peek(pending[0].approval_id) is pending[0]
        assert not writes and not session.notes_route_post_jobs
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_recovery_fails_closed_when_review_was_retired(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    pending = []
    await open_pending_note(monkeypatch, pending, session, store)
    try:
        response = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        _ = [frame async for frame in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        assert agent_runs.get_recoverable_notes_review(session.id) is not None
        store.retire_for_session(owner="alice", session_id=session.id)
        with pytest.raises(HTTPException) as status_err:
            await endpoints["/api/chat/stream_status/{session_id}"](
                request({}), session.id,
            )
        assert status_err.value.status_code == 404
        with pytest.raises(HTTPException) as resume_err:
            await endpoints["/api/chat/resume/{session_id}"](
                request({}), session.id,
            )
        assert resume_err.value.status_code == 409
        assert store.peek(pending[0].approval_id) is None
        assert not writes and not session.notes_route_post_jobs
        assert ports.operations.count("save_user") == 1
    finally:
        await clear_runs(session.id)
