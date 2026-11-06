"""Detached persisted notes Web SSE uses one original typed run and sealed store.

No production HTTP route, live model, external DB, or real notes are used.
Reconnections cannot mint a second tool effect or receipt.
"""

import asyncio
from dataclasses import replace
import json

import pytest

from src import agent_runs
from src import turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import ToolApprovalTurnRequest
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_native_tool_persistence import native_fixture
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs


def decode(frame):
    lines = frame.splitlines()
    cursor = next((line[4:] for line in lines if line.startswith("id: ")), None)
    body = next((line[6:] for line in lines if line.startswith("data: ")), None)
    if body == "[DONE]":
        return cursor, {"type": "DONE"}
    if body:
        return cursor, json.loads(body)
    return cursor, None


def create(monkeypatch):
    admission, session, ports, opts, writes, partials = native_fixture(monkeypatch)
    opts["approval_store"] = ToolApprovalStore()
    return GatewayTurns(TurnService()), admission, session, ports, opts, writes, partials


@pytest.mark.asyncio
async def test_disconnect_resumes_exact_sse_cursor_without_second_model_or_save(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    go = asyncio.Event()
    invoked = []

    async def model(_url, _name, _messages, **_kwargs):
        invoked.append(1)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        await go.wait()
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0, "output": "PRIVATE_NOTES",
        })
        yield 'data: {"delta":"The notes are ready."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        original = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        subscriber = original.events
        first = await asyncio.wait_for(anext(subscriber), timeout=5)
        first_cursor, first_payload = decode(first)
        assert first_payload["type"] == "tool_start"
        assert first_cursor == f"{original.run_id}:0"
        await subscriber.aclose()
        run = agent_runs.get_matching_run(session.id, original.run_id, mode="typed")
        assert run is not None and run.web_native_notes
        assert invoked == [1] and not writes

        go.set()
        await asyncio.wait_for(run.task, timeout=5)
        assert run.status == "done"
        assert len(writes) == 1
        resumed = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=original.run_id, after_cursor=first_cursor,
            approval_store=opts["approval_store"],
        )
        assert resumed is not None and resumed.run_id == original.run_id
        suffix = [decode(frame) async for frame in resumed.events]
        assert [payload.get("type", "delta" if "delta" in payload else None)
                for _, payload in suffix] == [
                    "tool_output", "delta", "message_saved", "final_response", "DONE",
                ]
        assert suffix[0][0] == f"{original.run_id}:1"
        assert suffix[-2][1]["content"] == "The notes are ready."
        assert suffix[-3][1]["id"] == session.history[-1].metadata["_db_id"]
        assert invoked == [1] and len(writes) == 1
        assert ports.operations.count("save_user") == 1
        assert [msg.role for msg in session.history] == ["user", "assistant"]
        assert "PRIVATE_NOTES" not in repr(suffix)
        assert not partials

        finished = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=original.run_id,
            after_cursor=f"{original.run_id}:{len(run.buffer) - 1}",
            approval_store=opts["approval_store"],
        )
        assert finished is not None
        assert [item async for item in finished.events] == []
    finally:
        go.set()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_asgi_detached_start_and_exact_cursor_resume_do_not_repeat_effect(monkeypatch):
    """Exercise real SSE response headers/frames, with test-only ASGI routes."""
    import httpx
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse

    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    effects = []

    async def model(_url, _name, _messages, **_kwargs):
        effects.append(True)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Notes from the original run."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    app = FastAPI()

    @app.post("/test-only/notes/start")
    async def start():
        delivery = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        return StreamingResponse(
            delivery.events, media_type="text/event-stream",
            headers={"X-Odysseus-Run-Id": delivery.run_id},
        )

    @app.get("/test-only/notes/resume")
    async def resume(request: Request):
        delivery = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=request.headers["X-Odysseus-Run-Id"],
            after_cursor=request.headers.get("Last-Event-ID"),
            approval_store=opts["approval_store"],
        )
        if delivery is None:
            raise HTTPException(404)
        return StreamingResponse(
            delivery.events, media_type="text/event-stream",
            headers={"X-Odysseus-Run-Id": delivery.run_id},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://example.com",
    ) as client:
        try:
            started = await client.post("/test-only/notes/start")
            assert started.status_code == 200
            run_id = started.headers["X-Odysseus-Run-Id"]
            assert len(run_id) == 32
            chunks = [block for block in started.text.split("\n\n") if block]
            first_cursor, first_payload = decode(chunks[0] + "\n\n")
            assert first_cursor == f"{run_id}:0"
            assert first_payload["type"] == "tool_start"
            restored = await client.get(
                "/test-only/notes/resume",
                headers={
                    "X-Odysseus-Run-Id": run_id,
                    "Last-Event-ID": first_cursor,
                },
            )
            assert restored.status_code == 200
            assert restored.headers["X-Odysseus-Run-Id"] == run_id
            resumed = [decode(x + "\n\n") for x in restored.text.split("\n\n") if x]
            assert [v.get("type", "delta" if "delta" in v else "?") for _, v in resumed] == [
                "tool_output", "delta", "message_saved", "final_response", "DONE",
            ]
            assert not any(v.get("type") == "tool_start" for _, v in resumed)
            assert effects == [True]
            assert len(writes) == 1 and not partials
            assert ports.operations.count("save_user") == 1
            assert "PRIVATE" not in restored.text
        finally:
            await clear_runs(session.id)


@pytest.mark.asyncio
async def test_pending_review_card_replays_with_terminal_cursor_and_not_twice(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    store = opts["approval_store"]
    pending = []
    invoked = []

    async def model(_url, _name, _messages, **kwargs):
        invoked.append("propose")
        content = '{"action":"list"}'
        approval = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="detached-notes-test", tool_name="manage_notes",
            content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(approval)
        yield approval_requested_event(approval)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": None, "output": "PRIVATE_REVIEW_RESULT",
        }, result={"ask_user": approval.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        original = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        # The Web subscriber may close before the background task finishes.
        await original.events.aclose()
        run = agent_runs.get_matching_run(session.id, original.run_id, mode="typed")
        assert run is not None
        await asyncio.wait_for(run.task, timeout=5)
        assert run.status == "done" and run.outcome.state == "awaiting_approval"
        assert invoked == ["propose"]
        assert len(pending) == 1 and store.peek(pending[0].approval_id) is pending[0]
        assert pending[0].origin_profile == "notes_v1"
        from src.tool_approvals import ExactToolApproval
        assert not ExactToolApproval(
            replace(pending[0], origin_profile=""),
        ).matches(
            owner="alice", session_id=session.id,
            tool_name="manage_notes", content=pending[0].content,
            workspace=None,
        )
        assert ports.operations.count("save_user") == 1
        assert not writes and not partials

        replay = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=original.run_id,
            approval_store=store,
        )
        assert replay is not None
        seen = [decode(frame) async for frame in replay.events]
        assert [item["type"] for _, item in seen] == ["ask_user", "DONE"]
        assert seen[0][1]["data"] == pending[0].public_payload()
        review_cursor = seen[0][0]
        assert review_cursor == f"{original.run_id}:{len(run.buffer) - 1}"
        assert "PRIVATE_REVIEW_RESULT" not in repr(seen)
        again = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=original.run_id,
            after_cursor=review_cursor, approval_store=store,
        )
        assert again is not None
        assert [frame async for frame in again.events] == []
        assert store.peek(pending[0].approval_id) is pending[0]
        assert invoked == ["propose"]
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_approved_continuation_starts_new_run_and_one_sealed_effect(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    store = opts["approval_store"]
    pending = []
    effects = []

    async def propose(_url, _name, _messages, **kwargs):
        content = '{"action":"list"}'
        action = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="original-notes-run",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(action)
        yield approval_requested_event(action)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None,
        }, result={"ask_user": action.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", propose)
    try:
        first = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        first_frames = [decode(frame) async for frame in first.events]
        assert [payload["type"] for _, payload in first_frames] == ["ask_user", "DONE"]
        assert len(pending) == 1
        assert len(session.history) == 1 and not writes

        async def approved(_url, _name, _messages, **kwargs):
            exact = kwargs["exact_approval"]
            assert exact.pending is pending[0]
            assert exact.claim(
                owner="alice", session_id=session.id, tool_name="manage_notes",
                content=pending[0].content, workspace=None,
            )
            effects.append("one exact action")
            yield tool_event_frame({
                "type": "tool_start", "tool": "manage_notes", "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes", "round": 1,
                "exit_code": 0,
            })
            yield 'data: {"delta":"Approved notes available."}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", approved)
        opts["request"] = ToolApprovalTurnRequest(pending[0].approval_id, "approve")
        second = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        second_frames = [decode(frame) async for frame in second.events]
        assert [p.get("type", "delta" if "delta" in p else "?") for _, p in second_frames] == [
            "tool_approval_resolved", "tool_start", "tool_output", "delta",
            "message_saved", "final_response", "DONE",
        ]
        assert second.run_id != first.run_id
        assert effects == ["one exact action"]
        assert pending[0].origin_profile == "notes_v1"
        assert store.peek(pending[0].approval_id) is None
        assert ports.operations.count("save_user") == 1
        assert [msg.role for msg in session.history] == ["user", "assistant"]
        assert len(writes) == 1 and not partials
        assert gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=first.run_id, approval_store=store,
        ) is None
        with pytest.raises(AdmissionDenied):
            gateway.start_detached_persisted_notes_web_sse(
                admission, session, **opts,
            )
        assert effects == ["one exact action"]
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_run_origin_cursor_and_stop_are_isolated(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    release = asyncio.Event()

    async def model(_url, _name, _messages, **_kwargs):
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        await release.wait()
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0,
        })
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        start = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        first = await asyncio.wait_for(anext(start.events), timeout=5)
        assert decode(first)[0] == f"{start.run_id}:0"
        assert gateway.resume_composed_detached_text_events(
            admission, session, context_owner="alice",
            expected_run_id=start.run_id, web_strict_text=True,
        ) is None
        assert gateway.resume_composed_detached_text_events(
            admission, session, context_owner="alice",
            expected_run_id=start.run_id,
        ) is None
        assert gateway.stop_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id="f" * 32,
        ) is False
        assert agent_runs.is_active(session.id)
        assert gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id="f" * 32,
            approval_store=opts["approval_store"],
        ) is None
        with pytest.raises(ValueError):
            gateway.resume_detached_persisted_notes_web_sse(
                admission, session, context_owner="alice",
                expected_run_id=start.run_id,
                after_cursor="f" * 32 + ":0",
                approval_store=opts["approval_store"],
            )
        with pytest.raises(ValueError):
            gateway.resume_detached_persisted_notes_web_sse(
                admission, session, context_owner="alice",
                expected_run_id=start.run_id,
                after_cursor=f"{start.run_id}:9999",
                approval_store=opts["approval_store"],
            )
        with pytest.raises(AdmissionDenied):
            gateway.resume_detached_persisted_notes_web_sse(
                admission, session, context_owner="mallory",
                expected_run_id=start.run_id,
                approval_store=opts["approval_store"],
            )
        with pytest.raises(AdmissionDenied):
            gateway.start_detached_persisted_notes_web_sse(
                admission, session, **opts,
            )
        assert gateway.stop_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice", expected_run_id=start.run_id,
        )
        await asyncio.sleep(0)
        run = agent_runs.get_matching_run(session.id, start.run_id, mode="typed")
        assert run is not None and run.status == "stopped"
        assert not writes and not partials
    finally:
        release.set()
        await start.events.aclose()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_bearer_approval_rejected_before_replacing_pending_run(monkeypatch):
    from src.gateway_admission import GatewayAdmission, Principal, SessionOwnership

    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    store = opts["approval_store"]
    records = []
    async def pending_model(_url, _model, _messages, **kwargs):
        content = '{"action":"list"}'
        action = store.create(
            owner="alice", session_id=session.id, origin_run_id="human-only",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        records.append(action)
        yield approval_requested_event(action)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": None,
        }, result={"ask_user": action.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", pending_model)
    try:
        started = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        _ = [frame async for frame in started.events]
        bearer = GatewayAdmission().admit(
            Principal("alice", "bearer", frozenset({"chat"})), session.id,
            lambda _: SessionOwnership(owner_id="alice"),
        )
        opts["request"] = ToolApprovalTurnRequest(records[0].approval_id, "approve")
        with pytest.raises(AdmissionDenied):
            gateway.start_detached_persisted_notes_web_sse(
                bearer, session, **opts,
            )
        assert agent_runs.get_matching_run(session.id, started.run_id, mode="typed")
        assert store.peek(records[0].approval_id) is records[0]
        assert [item.role for item in session.history] == ["user"]
        assert not writes and not partials
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_note_web_cursor_rejects_noncontiguous_typed_origin_and_invalid_heartbeats():
    from src.gateway_turn_events import TurnEvent, TypedRunEvent
    from src.gateway_web_compat_events import serialize_plain_text_web_sse

    run_id = "b" * 32
    async def source():
        yield ": heartbeat 1\n\n"
        yield TypedRunEvent(
            run_id, run_id + ":0",
            TurnEvent(1, "message.delta", {"text": "First"}),
        )
        yield ": heartbeat 2\n\n"
        yield TypedRunEvent(
            run_id, run_id + ":1",
            TurnEvent(2, "message.saved", {"id": "saved-1"}),
        )
        yield TypedRunEvent(
            run_id, run_id + ":2",
            TurnEvent(3, "message.final", {"text": "First"}),
        )
        yield TypedRunEvent(
            run_id, run_id + ":3",
            TurnEvent(4, "turn.completed", {}),
        )
    frames = [
        f async for f in serialize_plain_text_web_sse(source(), replay=True)
    ]
    assert frames[0] == ": heartbeat 1\n\n"
    assert frames[2] == ": heartbeat 2\n\n"
    assert "id: " + run_id + ":3" in frames[-1]

    async def invalid():
        yield ": heartbeat 1\ndata: {\\\"tool_call\\\":true}\n\n"
    with pytest.raises(ValueError, match="Invalid typed replay event"):
        _ = [f async for f in serialize_plain_text_web_sse(invalid(), replay=True)]


@pytest.mark.asyncio
async def test_note_detached_writer_failure_never_confirms_uncommitted_reply(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    failures = []

    def broken_writer(*args, **kwargs):
        failures.append(True)
        raise RuntimeError("private failure detail")

    opts["persistence"] = replace(opts["persistence"], saver=broken_writer)
    async def model(_url, _name, _messages, **kwargs):
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Visible response"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        original = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        frames = [frame async for frame in original.events]
        assert len([x for x in frames if "event: error" in x]) == 1
        assert not any('"type":"message_saved"' in x for x in frames)
        assert not any('"type":"final_response"' in x for x in frames)
        assert not any("[DONE]" in x for x in frames)
        assert "private failure detail" not in repr(frames)
        current_run = agent_runs.get_matching_run(session.id, original.run_id, mode="typed")
        assert failures == [True], (current_run.outcome.state, current_run.outcome.failure_code, [item.type for item in current_run.buffer])
        assert len(session.history) == 1
        assert not writes and not partials
        replay = gateway.resume_detached_persisted_notes_web_sse(
            admission, session, context_owner="alice",
            expected_run_id=original.run_id,
            approval_store=opts["approval_store"],
        )
        assert replay is not None
        replayed = [item async for item in replay.events]
        assert len([x for x in replayed if "event: error" in x]) == 1
        assert failures == [True]
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_unvalidated_internal_typed_run_cannot_mint_notes_web_provenance(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    opts["settings"] = replace(
        opts["settings"], persist_verified_native_tools=False,
    )
    try:
        with pytest.raises(AdmissionDenied):
            gateway.open_composed_detached_text_events(
                admission, session, **opts, web_native_notes=True,
            )
        assert agent_runs.get_matching_run(
            session.id, "a" * 32, mode="typed",
        ) is None
        assert agent_runs.get_active_run(session.id) is None
        assert not ports.operations and not writes and not partials
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_notes_profile_cannot_consume_sealed_legacy_origin_even_if_owned(monkeypatch):
    """A leaked owned legacy review ID never becomes a notes_v1 grant."""
    from src.tool_approvals import bind_gateway_approval_origin

    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    store = opts["approval_store"]
    approvals = []

    async def pending_model(_url, _name, _messages, **kwargs):
        content = '{"action":"list"}'
        # Simulate a legitimate legacy-origin proposal in this session;
        # the real Gateway will mark its own approvals notes_v1.
        with bind_gateway_approval_origin(""):
            pending = store.create(
                owner="alice", session_id=session.id,
                origin_run_id="legacy-before-gateway",
                tool_name="manage_notes", content=content, workspace=None,
                request_text="List my notes", continuation_query="List my notes",
                external_untrusted_context_seen=True,
                capabilities=capabilities_for_action("manage_notes", content),
                selected_tools={"manage_notes"},
                request_authority=kwargs["request_authority"],
            )
        approvals.append(pending)
        yield approval_requested_event(pending)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None,
        }, result={"ask_user": pending.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", pending_model)
    try:
        first = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        _ = [frame async for frame in first.events]
        assert len(approvals) == 1
        assert approvals[0].origin_profile == ""
        opts["request"] = ToolApprovalTurnRequest(approvals[0].approval_id, "approve")
        with pytest.raises(AdmissionDenied) as blocked:
            gateway.start_detached_persisted_notes_web_sse(
                admission, session, **opts,
            )
        assert blocked.value.status_code == 409
        assert store.peek(approvals[0].approval_id) is approvals[0]
        assert [item.role for item in session.history] == ["user"]
        assert ports.operations.count("save_user") == 1
        assert not writes and not partials
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_replay_rejects_cross_adapter_and_stale_card_after_consumption(monkeypatch):
    gateway, admission, session, ports, opts, writes, partials = create(monkeypatch)
    store = opts["approval_store"]
    pending = []

    async def propose(_url, _name, _messages, **kwargs):
        content = '{"action":"list"}'
        action = store.create(
            owner="alice", session_id=session.id, origin_run_id="prior-run",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(action)
        yield approval_requested_event(action)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None,
        }, result={"ask_user": action.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", propose)
    try:
        run = gateway.start_detached_persisted_notes_web_sse(
            admission, session, **opts,
        )
        _ = [frame async for frame in run.events]
        assert store.peek(pending[0].approval_id) is pending[0]
        # Simulate a separately resolved action without starting a new run:
        # the original replay must not resurrect a stale actionable card.
        store.retire_for_session(owner="alice", session_id=session.id)
        with pytest.raises(AdmissionDenied) as rejected:
            gateway.resume_detached_persisted_notes_web_sse(
                admission, session, context_owner="alice",
                expected_run_id=run.run_id, approval_store=store,
            )
        assert rejected.value.status_code == 409
        assert store.peek(pending[0].approval_id) is None
    finally:
        await clear_runs(session.id)
