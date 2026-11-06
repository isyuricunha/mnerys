"""The guarded notes_v1 HTTP route cannot bypass its durable SQL decision fence.

Use only private in-memory SQLite and original test-only admitted sessions.
No migrations against configured databases, providers or live tool effects.
"""

import pytest
from fastapi import HTTPException

import routes.chat_routes as routes
from src import turn_service as runtime
from src.gateway_notes_decision_fence import NotesDecisionFence
from src.tool_capabilities import capabilities_for_action
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.tool_approvals import ToolApprovalStore
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs
from tests.test_mnerys_gateway_web_native_notes_integration import (
    request, route_fixture,
)


@pytest.mark.asyncio
async def test_original_review_is_registered_before_card_and_reserved_before_effect(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    fence = routes.server_notes_decision_fence()
    proposals = []
    phases = []

    async def proposal(_url, _model, _msgs, **kw):
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="http-decision-fence-test",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kw["request_authority"],
        )
        proposals.append(record)
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": record.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", proposal)
    try:
        first = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        first_frames = [x async for x in first.body_iterator]
        assert len(proposals) == 1 and '"type":"ask_user"' in "".join(first_frames)
        pending = proposals[0]
        status = fence.status(
            owner="alice", session_id=session.id, approval_id=pending.approval_id,
        )
        assert status is not None and status.state == "pending"
        assert len(session.history) == 1 and session.history[0].role == "user"

        async def continuation(_url, _model, _msgs, **kw):
            proof = kw["exact_approval"]
            assert proof.pending is pending
            reserved = fence.status(
                owner="alice", session_id=session.id,
                approval_id=pending.approval_id,
            )
            assert reserved.state == "reserved"
            phases.append("sql_reserved_before_original_action")
            assert proof.claim(
                owner="alice", session_id=session.id,
                tool_name="manage_notes", content=pending.content,
                workspace=None,
            )
            phases.append("one_effect")
            yield tool_event_frame({
                "type": "tool_start", "tool": "manage_notes", "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes",
                "round": 1, "exit_code": 0, "output": "PRIVATE",
            })
            yield 'data: {"delta":"Confirmed."}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", continuation)
        second = await endpoints["/api/chat_stream"](
            request({
                "tool_approval_id": pending.approval_id,
                "tool_approval_decision": "approve_task",
            }),
        )
        second_frames = [x async for x in second.body_iterator]
        assert '"type":"message_saved"' in "".join(second_frames)
        assert phases == ["sql_reserved_before_original_action", "one_effect"]
        final = fence.status(
            owner="alice", session_id=session.id, approval_id=pending.approval_id,
        )
        assert final.state == "completed"
        assert final.assistant_receipt == session.history[-1].metadata["_db_id"]
        assert len(writes) == len(session.notes_route_post_jobs) == 1
        assert store.peek(pending.approval_id) is None
        assert not partials
        with pytest.raises(HTTPException) as replay:
            await endpoints["/api/chat_stream"](
                request({
                    "tool_approval_id": pending.approval_id,
                    "tool_approval_decision": "approve_task",
                }),
            )
        assert replay.value.status_code in (403, 409)
        assert phases.count("one_effect") == 1
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_pending_sql_registration_failure_does_not_show_actionable_card(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    fence = routes.server_notes_decision_fence()
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append("registration")
        return False

    monkeypatch.setattr(fence, "issue", unavailable)

    async def proposal(_url, _model, _msgs, **kw):
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id, origin_run_id="no-ledger-record",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kw["request_authority"],
        )
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": record.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", proposal)
    try:
        first = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        frames = [x async for x in first.body_iterator]
        assert calls == ["registration"]
        assert '"type":"ask_user"' not in repr(frames)
        assert '"type":"message_saved"' not in repr(frames)
        assert any("event: error" in f for f in frames)
        assert "PRIVATE" not in repr(frames)
        assert len(session.history) == 1 and not writes and not partials
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_writer_failure_after_one_effect_leaves_durable_indeterminate_reservation(
    monkeypatch,
):
    """A crash-like ambiguous commit never becomes an automatic second effect."""
    from tests.test_mnerys_gateway_web_notes_ui_recovery import open_pending_note

    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    fence = routes.server_notes_decision_fence()
    records = []
    await open_pending_note(monkeypatch, records, session, store)
    effects = []

    try:
        started = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        frames = [frame async for frame in started.body_iterator]
        assert any('"type":"ask_user"' in frame for frame in frames)
        pending = records[0]
        assert fence.status(
            owner="alice", session_id=session.id,
            approval_id=pending.approval_id,
        ).state == "pending"

        def commit_but_fail(*_args, **_kwargs):
            # No successful receipt is available; the fence must NOT
            # advance to completed or authorize a retry.
            raise RuntimeError("synthetic private database failure")

        monkeypatch.setattr(routes, "save_assistant_response", commit_but_fail)

        async def approved_model(_url, _model, _msgs, **kwargs):
            exact = kwargs["exact_approval"]
            assert exact.pending is pending
            assert fence.status(
                owner="alice", session_id=session.id,
                approval_id=pending.approval_id,
            ).state == "reserved"
            assert exact.claim(
                owner="alice", session_id=session.id,
                tool_name="manage_notes", content=pending.content,
                workspace=None,
            )
            effects.append("exact_action_once")
            yield tool_event_frame({
                "type": "tool_start", "tool": "manage_notes", "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes",
                "round": 1, "exit_code": 0, "output": "PRIVATE",
            })
            yield 'data: {"delta":"Uncommitted visible text."}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", approved_model)
        continued = await endpoints["/api/chat_stream"](
            request({
                "tool_approval_id": pending.approval_id,
                "tool_approval_decision": "approve_task",
            }),
        )
        failed = [frame async for frame in continued.body_iterator]
        assert len([frame for frame in failed if "event: error" in frame]) == 1
        assert not any('"type":"message_saved"' in frame for frame in failed)
        assert not any('"type":"final_response"' in frame for frame in failed)
        assert "synthetic private" not in repr(failed)
        assert effects == ["exact_action_once"]
        assert not writes and not session.notes_route_post_jobs
        # An independent object simulates a worker's SQL read after restart.
        second_worker = NotesDecisionFence(fence._engine)
        assert second_worker.status(
            owner="alice", session_id=session.id,
            approval_id=pending.approval_id,
        ).state == "reserved"
        with pytest.raises(HTTPException) as replay:
            await endpoints["/api/chat_stream"](
                request({
                    "tool_approval_id": pending.approval_id,
                    "tool_approval_decision": "approve_task",
                }),
            )
        assert replay.value.status_code in (403, 409)
        assert effects == ["exact_action_once"]
        assert len(session.history) == 1
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_missing_schema_rejected_before_user_write_or_model_call(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    def nonexistent_schema():
        raise RuntimeError("private configured database address")

    monkeypatch.setattr(routes, "server_notes_decision_fence", nonexistent_schema)
    with pytest.raises(HTTPException) as denied:
        await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
    assert denied.value.status_code == 503
    assert "private" not in str(denied.value.detail)
    assert session.history == [] and not ports.operations
    assert not writes and not partials
    assert store.peek("a" * 43) is None


@pytest.mark.asyncio
async def test_durable_fence_denies_a_replayed_review_before_store_consume(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    fence = routes.server_notes_decision_fence()
    records = []
    async def proposal(_url, _model, _msgs, **kw):
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id, origin_run_id="reserved-restart-test",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kw["request_authority"],
        )
        records.append(record)
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": record.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", proposal)
    try:
        first = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        _ = [f async for f in first.body_iterator]
        pending = records[0]
        receipt = session.history[-1].metadata["_db_id"]
        assert fence.reserve(
            owner="alice", session_id=session.id,
            approval_id=pending.approval_id, action_digest=pending.digest,
            origin_user_receipt=receipt,
            decision="approve_task",
        )
        # Simulate the SQL record outliving a crashed worker, while the
        # original in-memory store in the *same* test is still present.
        with pytest.raises(HTTPException) as denied:
            await endpoints["/api/chat_stream"](
                request({
                    "tool_approval_id": pending.approval_id,
                    "tool_approval_decision": "approve_task",
                }),
            )
        assert denied.value.status_code == 409
        assert store.peek(pending.approval_id) is pending
        assert len(session.history) == 1 and not writes and not partials
    finally:
        await clear_runs(session.id)
