"""No-HTTP M2.1 plain-text Web SSE parity using real Gateway Core/writers.

This explicitly does not turn on the production Web migration. All models,
sessions, saved receipts and replay buffers are disposable in-process fixtures.
"""

import asyncio
from dataclasses import replace
import json

import pytest

from core.models import ChatMessage
from src import agent_runs
from src import turn_service as runtime
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedRunEvent
from src.gateway_turns import GatewayTurns
from src.gateway_web_compat_events import (
    serialize_plain_text_turn_event,
    serialize_plain_text_web_sse,
)
from src.turn_service import TurnService
from tests.test_mnerys_gateway_text_persistence import persisted_fixture
from tests.test_mnerys_gateway_turn_composer import fixture


async def source(*events):
    for event in events:
        yield event


async def frames(stream, **kwargs):
    return [frame async for frame in serialize_plain_text_web_sse(
        stream, **kwargs,
    )]


def decoded(frame):
    lines = frame.splitlines()
    data_line = next(line for line in lines if line.startswith("data: "))
    return json.loads(data_line[6:])


@pytest.mark.asyncio
async def test_direct_existing_writer_yields_web_recognized_receipt_before_final(
    monkeypatch,
):
    admission, session, ports, kw, saved = persisted_fixture()
    kw["persistence"] = replace(
        kw["persistence"], emit_message_saved_events=True,
    )
    calls = []

    async def model(_url, _model, messages, **options):
        calls.append((messages, options))
        yield 'data: {"delta":"Um texto em PT-BR."}\n\n'
        yield "data: [DONE]\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    observed = []

    async def capture():
        async for event in delivery.events:
            observed.append(event)
            yield event

    output = await frames(capture())
    assert [event.type for event in observed] == [
        "message.delta", "message.saved", "message.final", "turn.completed",
    ]
    assert [event.sequence for event in observed] == [1, 2, 3, 4]
    assert delivery.outcome.last_sequence == 4
    assert delivery.outcome.state == "completed"
    assert output[0] == 'data: {"delta":"Um texto em PT-BR."}\n\n'
    assert decoded(output[1]) == {
        "type": "message_saved", "id": "fixture-assistant-2",
    }
    assert decoded(output[2]) == {
        "type": "final_response", "content": "Um texto em PT-BR.",
    }
    assert output[3] == "data: [DONE]\n\n"
    assert [msg.role for msg in session.history] == ["user", "assistant"]
    assert session.messages[-1]["content"] == "Um texto em PT-BR."
    assert len(calls) == 1
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_writer_failure_emits_one_sanitized_web_error_without_done_or_receipt(
    monkeypatch,
):
    admission, session, ports, kw, saved = persisted_fixture()
    def failing_saver(*args, **kwargs):
        raise RuntimeError("sensitive credential should never appear in SSE")

    kw["persistence"] = replace(
        kw["persistence"], saver=failing_saver,
        emit_message_saved_events=True,
    )

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Visible draft"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    result = await frames(delivery.events)
    assert decoded(result[0]) == {"delta": "Visible draft"}
    assert len(result) == 2
    assert result[-1].startswith("event: error\n")
    assert decoded(result[-1]) == {
        "error": "The request could not be completed.", "status": 500,
    }
    assert delivery.outcome.state == "failed"
    assert delivery.outcome.failure_code == "persistence_error"
    assert all("message_saved" not in item and "[DONE]" not in item for item in result)
    assert "sensitive credential" not in repr(result)
    assert saved == []


@pytest.mark.asyncio
async def test_bad_assistant_receipt_is_never_accepted_as_web_saved_message(
    monkeypatch,
):
    admission, session, ports, kw, saved = persisted_fixture()

    def invalid_receipt(current, manager, session_id, response, metrics, **meta):
        current.history.append(ChatMessage(
            "assistant", response,
            metadata={"_db_id": "unsafe\nid"},
        ))

    kw["persistence"] = replace(
        kw["persistence"], saver=invalid_receipt,
        emit_message_saved_events=True,
    )

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Draft"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    output = await frames(delivery.events)
    assert len(output) == 2
    assert output[-1].startswith("event: error\n")
    assert delivery.outcome.failure_code == "persistence_error"
    assert all("unsafe" not in frame and "message_saved" not in frame for frame in output)
    assert saved == []


@pytest.mark.asyncio
async def test_non_persisted_direct_turn_cannot_be_presented_as_saved_web_reply(
    monkeypatch,
):
    admission, session, ports, kw = fixture()

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Draft only"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    output = []
    with pytest.raises(ValueError, match="no confirmed assistant receipt"):
        async for chunk in serialize_plain_text_web_sse(delivery.events):
            output.append(chunk)
    assert output == ['data: {"delta":"Draft only"}\n\n']
    assert session.messages == []


@pytest.mark.asyncio
async def test_default_direct_persistence_event_contract_is_unchanged(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Original contract"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    delivered = [event async for event in delivery.events]
    assert [event.type for event in delivered] == [
        "message.delta", "message.final", "turn.completed",
    ]
    assert [event.sequence for event in delivered] == [1, 2, 3]
    assert delivery.outcome.last_sequence == 3


@pytest.mark.asyncio
async def test_control_characters_and_sse_injection_stay_in_json_text():
    injected = "before\n\nid: forged:5\nevent: error\ndata: leaked\n\nend"
    delivered = await frames(source(
        TurnEvent(1, "message.delta", {"text": injected}),
        TurnEvent(2, "message.saved", {"id": "safe-123"}),
        TurnEvent(3, "message.final", {"text": injected}),
        TurnEvent(4, "turn.completed", {}),
    ))
    assert len(delivered) == 4
    assert decoded(delivered[0]) == {"delta": injected}
    assert decoded(delivered[2]) == {
        "type": "final_response", "content": injected,
    }
    assert not any("\nevent: error\n" in frame for frame in delivered)
    assert all(frame.count("\n\n") == 1 for frame in delivered)


@pytest.mark.asyncio
async def test_core_selected_model_has_safe_browser_fallback_shape():
    output = await frames(source(
        TurnEvent(1, "model.route.selected", {
            "selected_model": "fixture/primary",
            "answering_model": "fixture/backup",
            "candidate_index": 1,
            "round": 2,
        }),
        TurnEvent(2, "metrics", {"input_tokens": 15, "output_tokens": 7}),
        TurnEvent(3, "message.saved", {"id": "fixture-3"}),
        TurnEvent(4, "message.final", {"text": "Done"}),
        TurnEvent(5, "turn.completed", {}),
    ))
    assert decoded(output[0]) == {
        "type": "fallback",
        "selected_model": "fixture/primary",
        "answered_by": "fixture/backup",
        "candidate_index": 1, "round": 2,
    }
    assert decoded(output[1]) == {
        "type": "metrics",
        "data": {"input_tokens": 15, "output_tokens": 7},
    }
    assert output[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    TurnEvent(1, "tool.started", {"tool": "read_file"}),
    TurnEvent(1, "approval.requested", {"approval_id": "x" * 43}),
    TurnEvent(1, "turn.awaiting_approval", {"approval_id": "x" * 43}),
    TurnEvent(1, "document.updated", {"content": "private"}),
    TurnEvent(1, "model.route.selected", {
        "selected_model": "bad\nname", "answering_model": "backup",
        "candidate_index": 1, "round": 1,
    }),
    TurnEvent(1, "message.saved", {"id": "bad\nid"}),
    TurnEvent(1, "metrics", {"secret": 1}),
    TurnEvent(1, "turn.failed", {"code": "raw_error", "status": 403}),
])
async def test_unapproved_or_untrusted_event_never_becomes_web_action(event):
    with pytest.raises(ValueError):
        await frames(source(event))


@pytest.mark.asyncio
@pytest.mark.parametrize("typed,frame", [
    (TurnEvent(1, "thinking.delta", {"text": "Thinking about it"}),
     {"delta": "Thinking about it", "thinking": True}),
    (TurnEvent(1, "agent.step", {"round": 2}),
     {"type": "agent_step", "round": 2}),
    (TurnEvent(1, "turn.control", {"reason": "rounds_exhausted"}),
     {"type": "rounds_exhausted"}),
    (TurnEvent(1, "sources.web", {
        "sources": [{"url": "https://example.com/a", "title": "A"}],
    }), {"type": "web_sources", "data": [
        {"url": "https://example.com/a", "title": "A"},
    ]}),
])
async def test_display_families_map_to_the_browser_vocabulary(typed, frame):
    output = await frames(
        source(typed, TurnEvent(2, "turn.completed", {})),
        require_saved_receipt=False,
    )
    assert decoded(output[0]) == frame
    assert output[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    TurnEvent(1, "thinking.delta", {"text": ""}),
    TurnEvent(1, "thinking.delta", {"text": 7}),
    TurnEvent(1, "thinking.delta", {"delta": "wrong key"}),
    TurnEvent(1, "agent.step", {"round": 0}),
    TurnEvent(1, "turn.control", {"reason": "unknown_reason"}),
    TurnEvent(1, "sources.web", {"sources": "nope"}),
])
async def test_bad_display_families_fail_closed(bad):
    with pytest.raises(ValueError):
        await frames(source(bad), require_saved_receipt=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("events,match", [
    (
        [TurnEvent(1, "message.delta", {"text": "Draft"}),
         TurnEvent(3, "message.saved", {"id": "saved-1"})],
        "Out-of-order",
    ),
    (
        [TurnEvent(1, "message.saved", {"id": "saved-1"}),
         TurnEvent(2, "message.saved", {"id": "saved-1"})],
        "Duplicate",
    ),
    (
        [TurnEvent(1, "message.delta", {"text": "Incomplete"})],
        "without a terminal",
    ),
    (
        [TurnEvent(1, "message.saved", {"id": "saved-1"}),
         TurnEvent(2, "message.final", {"text": "Text"}),
         TurnEvent(3, "turn.completed", {"private": "test"})],
        "Unexpected completion",
    ),
])
async def test_bad_sequences_and_incomplete_streams_fail_closed(events, match):
    with pytest.raises(ValueError, match=match):
        await frames(source(*events))


@pytest.mark.asyncio
async def test_typed_detached_web_sse_replay_uses_existing_exact_run_cursors():
    session_id = "web-compat-replay-session"
    outcome = TurnOutcome()
    released = asyncio.Event()
    run = None

    async def typed_source():
        outcome.state = "running"
        for i, (kind, payload) in enumerate([
            ("message.delta", {"text": "Hi"}),
            ("message.saved", {"id": "fixture-assistant"}),
        ], start=1):
            outcome.last_sequence = i
            yield TurnEvent(i, kind, payload)
        await released.wait()
        outcome.last_sequence = 3
        yield TurnEvent(3, "message.final", {"text": "Hi"})
        outcome.state = "completed"
        outcome.last_sequence = 4
        yield TurnEvent(4, "turn.completed", {})

    try:
        turns = TurnService()
        run = turns.start_detached_typed(session_id, typed_source(), outcome)
        one = agent_runs.subscribe(
            session_id,
            agent_runs.get_matching_run(session_id, run.run_id, mode="typed"),
            typed=True,
        )
        first = await asyncio.wait_for(anext(one), timeout=3)
        assert first.cursor == f"{run.run_id}:0"
        await one.aclose()

        resumed = turns.resume_typed(
            session_id, run.run_id, after_cursor=first.cursor,
        )
        assert resumed is not None
        released.set()
        recovered = await frames(resumed.events, replay=True)
        assert recovered == [
            f'id: {run.run_id}:1\ndata: {{"type":"message_saved","id":"fixture-assistant"}}\n\n',
            f'id: {run.run_id}:2\ndata: {{"type":"final_response","content":"Hi"}}\n\n',
            f"id: {run.run_id}:3\ndata: [DONE]\n\n",
        ]
        assert len({value.splitlines()[0] for value in recovered}) == 3
        assert run.outcome.state == "completed"
        underlying = agent_runs.get_matching_run(
            session_id, run.run_id, mode="typed",
        )
        await asyncio.wait_for(underlying.task, timeout=3)
    finally:
        released.set()
        if run is not None:
            underlying = agent_runs._RUNS.pop(session_id, None)
            if underlying is not None:
                if underlying.task and not underlying.task.done():
                    underlying.task.cancel()
                    await asyncio.gather(underlying.task, return_exceptions=True)
                if underlying.evict_task and not underlying.evict_task.done():
                    underlying.evict_task.cancel()
                    await asyncio.gather(underlying.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_replay_rejects_foreign_cursor_and_mixed_modalities():
    runid = "a" * 32
    wrong = TypedRunEvent(
        run_id=runid,
        cursor="b" * 32 + ":0",
        event=TurnEvent(1, "message.saved", {"id": "safe"}),
    )
    with pytest.raises(ValueError, match="cursor"):
        await frames(source(wrong), replay=True)
    valid = TypedRunEvent(
        run_id=runid, cursor=runid + ":0",
        event=TurnEvent(1, "message.saved", {"id": "safe"}),
    )
    with pytest.raises(ValueError, match="representation"):
        await frames(source(valid), replay=False)
    with pytest.raises(ValueError, match="representation"):
        await frames(source(TurnEvent(1, "message.saved", {"id": "safe"})), replay=True)


@pytest.mark.asyncio
async def test_empty_direct_stream_is_not_a_successful_web_turn():
    with pytest.raises(ValueError, match="without a terminal"):
        await frames(source())


@pytest.mark.asyncio
async def test_gateway_preview_uses_the_same_composer_writer_and_browser_sse_contract(
    monkeypatch,
):
    admission, session, ports, kw, saved = persisted_fixture()
    gateway = GatewayTurns(TurnService())
    invoked = []

    async def model(_url, _model, messages, **kwargs):
        invoked.append((messages, kwargs))
        yield 'data: {"delta":"Rendered from Core."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    preview = gateway.preview_composed_plain_text_web_sse(
        admission, session, **kw,
    )
    output = [frame async for frame in preview]
    assert [decoded(frame) if "data: [DONE]" not in frame else "[DONE]"
            for frame in output] == [
        {"delta": "Rendered from Core."},
        {"type": "message_saved", "id": "fixture-assistant-2"},
        {"type": "final_response", "content": "Rendered from Core."},
        "[DONE]",
    ]
    assert len(saved) == 1
    assert ports.operations.count("save_user") == 1
    assert len(invoked) == 1
    assert session.history[-1].role == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "tools", "attachment", "workspace", "document",
    "approval", "no_persistence", "invalid_writer",
    "foreign_owner", "foreign_session",
])
async def test_gateway_web_preview_rejects_unsafe_inputs_before_context_io(
    invalid,
):
    from src.gateway_admission import AdmissionDenied
    from src.gateway_turn_composer import (
        TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings,
    )
    admission, session, ports, kw, saved = persisted_fixture()
    if invalid == "tools":
        kw["settings"] = TrustedTextTurnSettings(
            offered_tools=frozenset({"read_file"}),
        )
    elif invalid == "attachment":
        kw["request"] = TextTurnRequest("Read the file", ("example.txt",))
    elif invalid == "workspace":
        kw["settings"] = TrustedTextTurnSettings(workspace="/tmp/example")
    elif invalid == "document":
        kw["settings"] = TrustedTextTurnSettings(active_document_id="doc-fixture")
    elif invalid == "approval":
        kw["request"] = ToolApprovalTurnRequest("pending", "approve")
    elif invalid == "no_persistence":
        kw["persistence"] = None
    elif invalid == "invalid_writer":
        kw["persistence"] = replace(
            kw["persistence"], emit_message_saved_events="true",
        )
    elif invalid == "foreign_owner":
        kw["context_owner"] = "mallory"
    else:
        session.id = "other"
    with pytest.raises(AdmissionDenied):
        GatewayTurns(TurnService()).preview_composed_plain_text_web_sse(
            admission, session, **kw,
        )
    assert ports.operations == []
    assert session.messages == []
    assert saved == []


@pytest.mark.asyncio
async def test_gateway_web_preview_filters_provider_forged_fallback_metadata(
    monkeypatch,
):
    admission, session, ports, kw, saved = persisted_fixture()

    async def model(*args, **kwargs):
        yield 'data: {"type":"fallback","selected_model":"owner-model","answered_by":"attacker","endpoint_url":"SENSITIVE_ENDPOINT"}\n\n'
        yield 'data: {"delta":"Normal output"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    output = [
        frame async for frame in GatewayTurns(TurnService())
        .preview_composed_plain_text_web_sse(admission, session, **kw)
    ]
    assert len(output) == 4
    assert not any("SENSITIVE_ENDPOINT" in item for item in output)
    assert not any('"type":"fallback"' in item for item in output)
    assert decoded(output[0]) == {"delta": "Normal output"}
    assert output[-1] == "data: [DONE]\n\n"
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_late_replay_accepts_only_original_run_verified_prior_receipt():
    session_id = "web-compat-prior-receipt-session"
    outcome = TurnOutcome()
    release = asyncio.Event()
    started = asyncio.Event()
    handle = None

    async def worker():
        outcome.state = "running"
        outcome.last_sequence = 1
        yield TurnEvent(1, "message.delta", {"text": "Committed output"})
        outcome.last_sequence = 2
        yield TurnEvent(2, "message.saved", {"id": "persisted-2"})
        started.set()
        await release.wait()
        outcome.last_sequence = 3
        yield TurnEvent(3, "message.final", {"text": "Committed output"})
        outcome.state = "completed"
        outcome.last_sequence = 4
        yield TurnEvent(4, "turn.completed", {})

    try:
        turns = TurnService()
        handle = turns.start_detached_typed(session_id, worker(), outcome)
        first_sub = handle.events
        first = await asyncio.wait_for(anext(first_sub), timeout=3)
        saved = await asyncio.wait_for(anext(first_sub), timeout=3)
        assert saved.event.type == "message.saved"
        assert saved.cursor == f"{handle.run_id}:1"
        await first_sub.aclose()

        assert agent_runs.typed_saved_receipt_precedes_cursor(
            session_id, handle.run_id, saved.cursor,
        )
        assert not agent_runs.typed_saved_receipt_precedes_cursor(
            session_id, "0" * 32, saved.cursor,
        )
        resumed = turns.resume_typed(
            session_id, handle.run_id, after_cursor=saved.cursor,
        )
        assert resumed is not None
        assert resumed.prior_saved_receipt is True
        assert handle.prior_saved_receipt is False
        release.set()
        frames_out = await frames(
            resumed.events,
            replay=True,
            prior_saved_receipt=resumed.prior_saved_receipt,
        )
        assert frames_out == [
            f'id: {handle.run_id}:2\ndata: {{"type":"final_response","content":"Committed output"}}\n\n',
            f'id: {handle.run_id}:3\ndata: [DONE]\n\n',
        ]
        underlying = agent_runs.get_matching_run(
            session_id, handle.run_id, mode="typed",
        )
        await asyncio.wait_for(underlying.task, timeout=3)
    finally:
        release.set()
        run = agent_runs._RUNS.pop(session_id, None)
        if run is not None:
            if run.task and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            if run.evict_task and not run.evict_task.done():
                run.evict_task.cancel()
                await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_replay_without_prior_receipt_proof_fails_closed():
    runid = "b" * 32
    delayed = [
        TypedRunEvent(
            runid, runid + ":2",
            TurnEvent(3, "message.final", {"text": "Unconfirmed"}),
        ),
        TypedRunEvent(
            runid, runid + ":3",
            TurnEvent(4, "turn.completed", {}),
        ),
    ]
    with pytest.raises(ValueError, match="no confirmed assistant receipt"):
        await frames(source(*delayed), replay=True)
    with pytest.raises(TypeError, match="serialization options"):
        await frames(
            source(*delayed), replay=False, prior_saved_receipt=True,
        )
