"""Typed, process-local direct turn events from a filtered legacy SSE adapter."""

import asyncio
import inspect
import json

import pytest

from src.gateway_turn_events import (
    TurnEvent,
    TurnOutcome,
    translate_legacy_turn_events,
)


def open_test_events(chunks, *, on_close=None):
    async def source():
        try:
            for value in chunks:
                if isinstance(value, BaseException):
                    raise value
                yield value
        finally:
            if on_close is not None:
                on_close.append(True)

    outcome = TurnOutcome()
    return translate_legacy_turn_events(source(), outcome), outcome


async def collect_events(stream):
    return [e async for e in stream]


@pytest.mark.asyncio
async def test_typed_delta_final_and_completion_ignores_duplicate_done():
    stream, outcome = open_test_events([
        'data: {"delta":"Hi"}\n\n',
        'data: {"delta":" there"}\n\n',
        'data: [DONE]\n\n',
        'data: [DONE]\n\n',
    ])
    events = await collect_events(stream)
    assert [(x.sequence, x.type) for x in events] == [
        (1, "message.delta"), (2, "message.delta"),
        (3, "message.final"), (4, "turn.completed"),
    ]
    assert [x.payload for x in events] == [
        {"text": "Hi"}, {"text": " there"}, {"text": "Hi there"}, {},
    ]
    assert all(isinstance(x, TurnEvent) for x in events)
    assert outcome.state == "completed"
    assert outcome.content == "Hi there"
    assert outcome.last_sequence == 4
    assert outcome.failure_code is None


@pytest.mark.asyncio
async def test_fragmented_crlf_comments_ids_and_multiple_frames_are_normalized():
    stream, outcome = open_test_events([
        ": heartbeat\r", "\n\r", "\n",
        "id: run-fake:99\r\n",
        "data: {\"delta\":\"one\"}\r", "\n\r\n",
        'data: {"delta":"two"}\n\ndata: [DONE]\n\n',
    ])
    events = await collect_events(stream)
    assert [x.type for x in events] == [
        "message.delta", "message.delta", "message.final", "turn.completed",
    ]
    assert events[-2].payload == {"text": "onetwo"}
    assert outcome.state == "completed"
    assert "run-fake" not in repr(events)


@pytest.mark.asyncio
async def test_explicit_final_message_overrides_incremental_deltas():
    stream, result = open_test_events([
        'data: {"delta":"Draft"}\n\n',
        'data: {"type":"final_response","content":"Finished answer"}\n\n',
        'data: [DONE]\n\n',
    ])
    events = await collect_events(stream)
    assert events[-2].type == "message.final"
    assert events[-2].payload == {"text": "Finished answer"}
    assert result.content == "Finished answer"
    assert result.state == "completed"


@pytest.mark.asyncio
async def test_usage_exposes_only_nonnegative_integer_counts():
    stream, result = open_test_events([
        'data: {"type":"usage","data":{"input_tokens":12,"output_tokens":3,"total_tokens":15,"model":"secret-provider","api_key":"secret-value","reasoning_tokens":7}}\n\n',
        'data: {"type":"usage","data":{"input_tokens":true,"output_tokens":-1,"total_tokens":"100"}}\n\n',
        'data: [DONE]\n\n',
    ])
    events = await collect_events(stream)
    assert [(x.type, x.payload) for x in events[:2]] == [
        ("usage", {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}),
        ("usage", {}),
    ]
    assert "secret" not in repr(events)
    assert result.state == "completed"


@pytest.mark.asyncio
async def test_unknown_tools_approvals_reasoning_and_provider_details_stay_private():
    sensitive_events = [
        {"type":"tool_result","output":"secret-output"},
        {"type":"approval_requested","args":{"token":"secret-token"}},
        {"type":"thinking","content":"secret-reasoning"},
        {"type":"delta","delta":"secret-reasoning","thinking":True},
        {"type":"delta","delta":"secret-reasoning","channel":"analysis"},
        {"type":"delta","delta":"secret-reasoning","reasoning_content":"secret-reasoning"},
        {"type":"reasoning.delta","delta":"secret-reasoning"},
        {"type":"model_actual","headers":{"Authorization":"secret-token"}},
    ]
    frames = ['data: '+json.dumps(e)+'\n\n' for e in sensitive_events]
    frames.extend([
        'data: {"delta":"Public answer"}\n\n',
        "data: [DONE]\n\n",
    ])
    stream, result = open_test_events(frames)
    events = await collect_events(stream)
    assert [e.type for e in events] == [
        "message.delta", "message.final", "turn.completed",
    ]
    assert result.content == "Public answer"
    assert "secret" not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"status":503,"error":"secret endpoint/private address"}',
    '{"status":false,"text":"secret"}',
    "not-json-secret",
])
async def test_provider_error_is_terminal_and_redacted_even_if_followed_by_done(raw):
    stream, outcome = open_test_events([
        'data: {"delta":"Visible fragment"}\n\n',
        "event: error\ndata: "+raw+"\n\n",
        "data: [DONE]\n\n",
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert events[-1].payload["code"] == "provider_error"
    assert events[-1].payload["status"] == (503 if '"status":503' in raw else None)
    assert "secret" not in repr(events)
    assert outcome.state == "failed"
    assert outcome.failure_code == "provider_error"
    assert outcome.content == ""


@pytest.mark.asyncio
async def test_agent_terminal_failure_is_sanitized_without_provider_diagnostics():
    stream, result = open_test_events([
        'data: {"delta":"Partial"}\n\n',
        'data: {"type":"agent_terminal","data":{"failure":{"status":429,"message":"secret provider body"},"tool_events":[{"content":"secret"}]}}\n\n',
        'event: error\ndata: {"error":"secret"}\n\n',
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert events[-1].payload == {"code": "agent_terminal", "status": 429}
    assert "secret" not in repr(events)
    assert result.state == "failed"


@pytest.mark.asyncio
async def test_premature_eof_is_not_misclassified_as_success():
    stream, result = open_test_events([
        'data: {"delta":"unfinished"}\n\n',
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert events[-1].payload == {"code": "incomplete_stream", "status": None}
    assert result.state == "failed"
    assert result.failure_code == "incomplete_stream"


@pytest.mark.asyncio
async def test_explicit_generator_close_cancels_and_closes_inner_stream():
    closed = []
    stream, result = open_test_events([
        'data: {"delta":"unfinished"}\n\n',
        "data: [DONE]\n\n",
    ], on_close=closed)
    assert (await anext(stream)).type == "message.delta"
    await stream.aclose()
    assert result.state == "cancelled"
    assert result.failure_code is None
    assert closed == [True]


@pytest.mark.asyncio
async def test_cancelled_upstream_propagates_and_cleans_inner_stream():
    closed = []
    stream, result = open_test_events([
        'data: {"delta":"partial"}\n\n',
        asyncio.CancelledError(),
    ], on_close=closed)
    assert (await anext(stream)).type == "message.delta"
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert result.state == "cancelled"
    assert closed == [True]


@pytest.mark.asyncio
async def test_provider_exception_propagates_without_success_status():
    stream, result = open_test_events([
        'data: {"delta":"partial"}\n\n',
        RuntimeError("test-only simulated provider crash"),
    ])
    with pytest.raises(RuntimeError):
        await collect_events(stream)
    assert result.state == "failed"
    assert result.failure_code == "stream_exception"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_chunk", [42, None, b"data: [DONE]\n\n"])
async def test_nontext_legacy_event_fails_closed(bad_chunk):
    stream, result = open_test_events([bad_chunk])
    with pytest.raises(ValueError, match="Invalid inherited"):
        await collect_events(stream)
    assert result.state == "failed"


@pytest.mark.asyncio
async def test_oversized_unterminated_legacy_frame_fails_closed():
    stream, result = open_test_events(["data: "+"x"*1_048_576])
    with pytest.raises(ValueError, match="safety limit"):
        await collect_events(stream)
    assert result.state == "failed"


@pytest.mark.asyncio
async def test_turn_outcome_cannot_be_reused_for_second_stream():
    result = TurnOutcome()
    stream, _ = open_test_events(['data: [DONE]\n\n'])
    await collect_events(stream)
    result.state = "completed"

    async def fake():
        yield "data: [DONE]\n\n"

    with pytest.raises(ValueError, match="cannot be reused"):
        await collect_events(translate_legacy_turn_events(fake(), result))




@pytest.mark.asyncio
async def test_oversized_terminated_frame_is_also_rejected_before_parsing():
    stream, result = open_test_events([
        'data: {"delta":"'+("x"*1_048_576)+'"}\n\n',
    ])
    with pytest.raises(ValueError, match="safety limit"):
        await collect_events(stream)
    assert result.state == "failed"
    assert result.failure_code == "stream_exception"


@pytest.mark.asyncio
async def test_cancel_after_completed_terminal_cannot_rewrite_result():
    stream, result = open_test_events([
        'data: {"delta":"Finished"}\n\n',
        "data: [DONE]\n\n",
        asyncio.CancelledError(),
    ])
    with pytest.raises(asyncio.CancelledError):
        await collect_events(stream)
    assert result.state == "completed"
    assert result.content == "Finished"
    assert result.failure_code is None
    assert result.last_sequence == 3




@pytest.mark.asyncio
async def test_native_core_terminal_completes_without_legacy_done_frame():
    from src.agent_core_events import completed_core_event

    stream, outcome = open_test_events([
        'data: {"delta":"Typed completion"}\n\n',
        completed_core_event(),
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == [
        "message.delta", "message.final", "turn.completed",
    ]
    assert events[-2].payload == {"text": "Typed completion"}
    assert outcome.state == "completed"
    assert outcome.content == "Typed completion"


@pytest.mark.asyncio
async def test_native_core_completion_keeps_explicit_final_response():
    from src.agent_core_events import completed_core_event

    stream, outcome = open_test_events([
        'data: {"delta":"Draft"}\n\n',
        'data: {"type":"final_response","content":"Approved visible text"}\n\n',
        completed_core_event(),
        completed_core_event(),
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == [
        "message.delta", "message.final", "turn.completed",
    ]
    assert outcome.content == "Approved visible text"
    assert outcome.last_sequence == 3


@pytest.mark.asyncio
async def test_native_core_terminal_cannot_recover_provider_failure():
    from src.agent_core_events import completed_core_event

    stream, outcome = open_test_events([
        'event: error\ndata: {"status":502,"error":"private upstream"}\n\n',
        completed_core_event(),
    ])
    events = await collect_events(stream)
    assert [e.type for e in events] == ["turn.failed"]
    assert outcome.state == "failed"
    assert "private upstream" not in repr(events)


@pytest.mark.asyncio
async def test_native_core_terminal_rejects_incomplete_preceding_sse_frame():
    from src.agent_core_events import completed_core_event

    stream, outcome = open_test_events([
        'data: {"delta":"unterminated"}',
        completed_core_event(),
    ])
    with pytest.raises(ValueError, match="Incomplete legacy frame"):
        await collect_events(stream)
    assert outcome.state == "failed"


@pytest.mark.asyncio
async def test_native_core_terminal_rejects_forged_unknown_signal():
    from src.agent_core_events import CoreTerminalEvent

    stream, outcome = open_test_events([
        CoreTerminalEvent("turn.failed"),
    ])
    with pytest.raises(ValueError, match="Invalid Agent Core"):
        await collect_events(stream)
    assert outcome.state == "failed"


def test_no_web_transport_exec_or_persistence_imports():
    import ast
    from src import gateway_turn_events
    tree = ast.parse(inspect.getsource(gateway_turn_events))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith((
                "routes", "fastapi", "starlette", "core.database",
                "src.tool_execution", "src.agent_loop",
            ))
        elif isinstance(node, ast.Import):
            assert all(
                not child.name.startswith((
                    "routes", "fastapi", "starlette", "core.database",
                    "src.tool_execution", "src.agent_loop",
                ))
                for child in node.names
            )
