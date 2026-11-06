"""Authenticated, non-actionable durable milestone replay on original notes route.

Uses only the native test fixture with isolated in-memory SQLite and a
synthetic model. The journal cannot authorize or reconstruct a tool.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.chat_routes as routes
from routes.gateway_notes_adapter import read_admitted_notes_event_journal
from src import turn_service as runtime
from src.gateway_admission import GatewayAdmission, Principal
from src.session_repository import SessionOwnership
from src.agent_tool_events import tool_event_frame
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs
from tests.test_mnerys_gateway_web_native_notes_integration import (
    request, route_fixture,
)
from tests.test_mnerys_gateway_web_notes_ui_recovery import open_pending_note


@pytest.mark.asyncio
async def test_persisted_safe_events_replay_after_lost_run_and_after_assistant_commit(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    records = []
    await open_pending_note(monkeypatch, records, session, store)
    try:
        started = await handlers["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        _ = [frame async for frame in started.body_iterator]
        journal_route = handlers["/api/chat/notes_journal/{session_id}"]
        first = await journal_route(request({}), session.id)
        assert first["gateway_profile"] == "notes_v1"
        assert len(first["next_cursor"]) == 32
        assert len(first["events"]) == 1
        assert first["events"][0]["kind"] == "review_registered"
        # A direct Gateway call without an explicit cursor must use the same
        # empty-string pagination default as the authenticated HTTP adapter.
        direct_events = read_admitted_notes_event_journal(
            admission=admission, session=session,
            journal=routes.server_notes_event_journal(),
        )
        assert [item.cursor for item in direct_events] == [
            first["events"][0]["cursor"]
        ]
        assert records[0].approval_id not in repr(first)
        assert records[0].content not in repr(first)
        assert not any("text" in x or "payload" in x for x in first["events"])

        # No model or tool is invoked while replaying a persisted metadata
        # milestone from a different server-side journal object.
        run = __import__("src.agent_runs", fromlist=["_RUNS"])
        forgotten = run._RUNS.pop(session.id)
        try:
            recovered = await journal_route(request({}), session.id)
            assert recovered == first
        finally:
            run._RUNS[session.id] = forgotten

        original = records[0]

        async def continuation(_url, _model, _msgs, **kwargs):
            proof = kwargs["exact_approval"]
            assert proof.pending is original
            assert proof.claim(
                owner="alice", session_id=session.id,
                tool_name="manage_notes", content=original.content,
                workspace=None,
            )
            yield tool_event_frame({
                "type": "tool_start", "tool": "manage_notes", "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes", "round": 1,
                "exit_code": 0, "output": "PRIVATE_TOOL_RESULT",
            })
            yield 'data: {"delta":"Confirmed."}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", continuation)
        approved = await handlers["/api/chat_stream"](
            request({
                "tool_approval_id": original.approval_id,
                "tool_approval_decision": "approve_task",
            }),
        )
        response = [frame async for frame in approved.body_iterator]
        assert any('"type":"message_saved"' in x for x in response)
        last = await journal_route(request({}), session.id)
        assert [ev["kind"] for ev in last["events"]] == [
            "review_registered", "decision_reserved", "assistant_committed",
        ]
        assert records[0].approval_id not in repr(last)
        assert "PRIVATE_TOOL_RESULT" not in repr(last)
        assert len(writes) == len(session.notes_route_post_jobs) == 1

        raw = request({})
        raw.query_params = {"after_cursor": str(first["next_cursor"]), "limit": "1"}
        newer = await journal_route(raw, session.id)
        assert [x["kind"] for x in newer["events"]] == ["decision_reserved"]
        assert newer["next_cursor"] != first["next_cursor"]
        assert len(newer["next_cursor"]) == 32
        # A syntactically valid but unknown token is not a 503 or an
        # invitation to restart a different user's timeline.
        other_scope_token = routes.server_notes_event_journal().append(
            owner="other-user", session_id=session.id,
            origin_user_receipt=session.history[-2].metadata["_db_id"],
            approval_id="B" * 43, kind="review_registered",
        )
        assert len(other_scope_token) == 32
        for invalid in (
            {"after_cursor": "-1"},
            {"after_cursor": "NaN"},
            {"after_cursor": "1", "limit": "0"},
            {"after_cursor": "X" * 32},
            {"after_cursor": other_scope_token},
            {"limit": "1000"},
        ):
            bad = request({})
            bad.query_params = invalid
            with pytest.raises(HTTPException) as denied:
                await journal_route(bad, session.id)
            assert denied.value.status_code == 400

        # The next user request is not the original receipt and cannot
        # inspect the prior turn's timeline even with a guessed cursor.
        session.history.append(SimpleNamespace(
            role="user", metadata={"_db_id": "another-user-receipt"},
        ))
        assert (await journal_route(request({}), session.id))["events"] == []
        session.history.pop()

        delegated = GatewayAdmission().admit(
            Principal("alice", "bearer", frozenset({"chat"})), session.id,
            lambda sid: SessionOwnership(owner_id="alice"),
        )
        monkeypatch.setattr(
            routes, "_verify_session_owner", lambda req, sid: delegated,
        )
        with pytest.raises(HTTPException) as bearer:
            await journal_route(request({}), session.id)
        assert bearer.value.status_code == 404
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_failed_journal_registration_cannot_expose_an_actionable_card(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    journal = routes.server_notes_event_journal()
    pending = []
    await open_pending_note(monkeypatch, pending, session, store)

    def fail_journal(**kwargs):
        raise RuntimeError("private journal connection string")

    monkeypatch.setattr(journal, "append", fail_journal)
    try:
        response = await handlers["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        frames = [x async for x in response.body_iterator]
        assert pending
        assert any("event: error" in x for x in frames)
        assert '"type":"ask_user"' not in repr(frames)
        assert "private journal" not in repr(frames)
        assert not writes and not partials
        assert len(session.history) == 1
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_failed_reservation_journal_does_not_replay_the_tool(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    journal = routes.server_notes_event_journal()
    fence = routes.server_notes_decision_fence()
    pending = []
    await open_pending_note(monkeypatch, pending, session, store)
    effects = []
    try:
        first = await handlers["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        _ = [x async for x in first.body_iterator]
        assert len(pending) == 1

        original_append = journal.append

        def fail_reserved(**kwargs):
            if kwargs["kind"] == "decision_reserved":
                raise RuntimeError("private journal failure")
            return original_append(**kwargs)

        monkeypatch.setattr(journal, "append", fail_reserved)

        async def must_not_dispatch(*args, **kwargs):
            effects.append("DISPATCHED")
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", must_not_dispatch)
        try:
            next_response = await handlers["/api/chat_stream"](
                request({
                    "tool_approval_id": pending[0].approval_id,
                    "tool_approval_decision": "approve_task",
                }),
            )
            frames = [x async for x in next_response.body_iterator]
            assert any("event: error" in x for x in frames)
        except HTTPException as denied:
            assert denied.status_code in (409, 500, 503)
        assert effects == []
        status = fence.status(
            owner="alice", session_id=session.id,
            approval_id=pending[0].approval_id,
        )
        assert status.state == "reserved"
        assert not writes and not session.notes_route_post_jobs
        assert [e.kind for e in journal.replay(
            owner="alice", session_id=session.id,
            origin_user_receipt=session.history[-1].metadata["_db_id"],
        )] == ["review_registered"]
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_journal_schema_failure_blocks_http_before_original_writer(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, handlers = (
        route_fixture(monkeypatch)
    )
    monkeypatch.setattr(
        routes, "server_notes_event_journal",
        lambda: (_ for _ in ()).throw(RuntimeError("private db url")),
    )
    with pytest.raises(HTTPException) as blocked:
        await handlers["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
    assert blocked.value.status_code == 503
    assert "private" not in str(blocked.value.detail)
    assert not writes and not partials and not session.history
    with pytest.raises(HTTPException) as read_blocked:
        await handlers["/api/chat/notes_journal/{session_id}"](
            request({}), session.id,
        )
    assert read_blocked.value.status_code == 503
