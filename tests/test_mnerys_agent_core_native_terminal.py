"""M2.1 native Agent Core completion signal, with unchanged default Web SSE.

The real inherited Agent Loop executes an isolated, model-free missing-
workspace early exit; both output formats use the same internal path.
"""

import ast
import inspect
from pathlib import Path

import pytest

from src.agent_core_events import (
    CoreTerminalEvent, CoreTextEvent, CoreMetricsEvent, CoreFailureEvent,
    completed_core_event,
    legacy_core_terminal_frame,
)
from src.gateway_turn_events import TurnOutcome, translate_legacy_turn_events


def test_core_terminal_signal_has_no_provider_or_authorization_payload():
    event = completed_core_event()
    assert type(event) is CoreTerminalEvent
    assert event.type == "turn.completed"
    assert not hasattr(event, "session_id")
    assert not hasattr(event, "owner")
    assert not hasattr(event, "tool_grants")
    assert legacy_core_terminal_frame(event) == "data: [DONE]\n\n"
    with pytest.raises(TypeError):
        legacy_core_terminal_frame(CoreTerminalEvent("turn.failed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_actual_agent_loop_emits_native_completion_only_when_opted_in(
    monkeypatch, native,
):
    import src.agent_loop as loop

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "estimate_tokens", lambda *_a, **_kw: 10)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda _owner: set())

    async def impossible_model(*_a, **_kw):
        raise AssertionError("Model must not be invoked on missing workspace")
        yield "unused"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", impossible_model)

    outputs = [x async for x in loop.stream_agent_loop(
        "https://example.com/v1", "test-model",
        [{"role": "user", "content": "In this workspace, fix a typo and verify it."}],
        max_rounds=1, relevant_tools=None, owner="alice", workspace=None,
        native_terminal_events=native,
    )]
    assert outputs[:-1]
    if native:
        assert any(
            isinstance(item, CoreTextEvent)
            and item.type == "message.delta"
            and "No active workspace is set" in item.text
            for item in outputs
        )
        assert any(isinstance(item, CoreMetricsEvent) for item in outputs)
        assert isinstance(outputs[-1], CoreTerminalEvent)
        assert outputs[-1].type == "turn.completed"
        assert not any('"missing_workspace": true' in str(item) for item in outputs)
    else:
        assert all(isinstance(item, str) for item in outputs)
        assert any("No active workspace is set" in item for item in outputs)
        assert any('"missing_workspace": true' in item for item in outputs)
        assert outputs[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_real_core_native_terminal_flows_to_typed_delivery_without_sse_done(
    monkeypatch,
):
    import src.agent_loop as loop

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "estimate_tokens", lambda *_a, **_kw: 10)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda _owner: set())

    async def impossible_model(*_a, **_kw):
        raise AssertionError("No LLM should run")
        yield "unused"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", impossible_model)

    stream = loop.stream_agent_loop(
        "https://example.com/v1", "test-model",
        [{"role": "user", "content": "In this workspace, fix a typo and verify it."}],
        max_rounds=1, owner="alice", workspace=None,
        native_terminal_events=True,
    )
    outcome = TurnOutcome()
    events = [item async for item in translate_legacy_turn_events(stream, outcome)]
    assert [item.type for item in events] == [
        "message.delta", "metrics", "message.final", "turn.completed",
    ]
    assert events[1].payload["input_tokens"] == 10
    assert events[1].payload["output_tokens"] > 0
    assert outcome.state == "completed"
    assert outcome.content.startswith("No active workspace is set.")
    assert "workspace set" in outcome.content


@pytest.mark.asyncio
async def test_actual_core_rejects_nonboolean_native_mode_before_any_model(
    monkeypatch,
):
    import src.agent_loop as loop
    called = []

    def called_setting(*_args, **_kw):
        called.append("configuration")
        return None

    monkeypatch.setattr(loop, "get_setting", called_setting)
    with pytest.raises(TypeError, match="terminal event flag"):
        [x async for x in loop.stream_agent_loop(
            "https://example.com/v1", "test-model",
            [{"role": "user", "content": "hello"}],
            native_terminal_events="yes",
        )]
    assert called == []




@pytest.mark.asyncio
async def test_native_terminal_disables_unapproved_teacher_child_escalation(
    monkeypatch,
):
    import src.teacher_escalation as teacher

    called = []

    async def should_not_start_teacher(**_kw):
        called.append("child-run")
        yield "unexpected child event"

    monkeypatch.setattr(teacher, "run_teacher_inline", should_not_start_teacher)

    @teacher.with_teacher_takeover
    async def fake_student():
        teacher.request_teacher_takeover(
            owner="alice", session_id="session-1", student_reply="test",
        )
        yield completed_core_event()

    result = [item async for item in fake_student()]
    assert result == [CoreTerminalEvent()]
    assert not called


@pytest.mark.asyncio
async def test_legacy_terminal_keeps_existing_teacher_child_behavior(
    monkeypatch,
):
    import src.teacher_escalation as teacher

    called = []

    async def fake_teacher(**_kw):
        called.append("child-run")
        yield 'data: {"delta":"teacher answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(teacher, "run_teacher_inline", fake_teacher)

    @teacher.with_teacher_takeover
    async def fake_student():
        teacher.request_teacher_takeover(
            owner="alice", session_id="session-1", student_reply="test",
        )
        yield "data: [DONE]\n\n"

    result = [item async for item in fake_student()]
    assert called == ["child-run"]
    assert result == [
        'data: {"delta":"teacher answer"}\n\n',
        "data: [DONE]\n\n",
    ]




@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_completion_gate_emits_filtered_text_and_metrics_without_provider_secrets(
    native,
):
    from src.agent_runtime.completion import with_completion_gate

    @with_completion_gate
    async def fake_inner(messages, native_terminal_events=False):
        yield 'data: {"delta":"Visible answer"}\n\n'
        yield 'data: {"type":"metrics","data":{"input_tokens":7,"output_tokens":9,"model":"SECRET_MODEL","api_key":"SECRET_KEY","thinking":"SECRET_REASONING"}}\n\n'
        yield completed_core_event() if native_terminal_events else "data: [DONE]\n\n"

    chunks = [x async for x in fake_inner(
        [{"role": "user", "content": "Explain a queue"}],
        native_terminal_events=native,
    )]
    if native:
        visible = [item for item in chunks if isinstance(item, CoreTextEvent)]
        metrics = [item for item in chunks if isinstance(item, CoreMetricsEvent)]
        assert [item.text for item in visible] == ["Visible answer"]
        assert len(metrics) == 1
        assert metrics[0].input_tokens == 7
        assert metrics[0].output_tokens == 9
        assert not hasattr(metrics[0], "api_key")
        assert not hasattr(metrics[0], "model")
        assert isinstance(chunks[-1], CoreTerminalEvent)
        assert "SECRET_" not in repr(chunks)
    else:
        assert all(isinstance(item, str) for item in chunks)
        assert chunks[-1] == "data: [DONE]\n\n"
        assert any("SECRET_MODEL" in item for item in chunks)
        # The Web route still owns and filters the original rich SSE metrics.


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["provider_error", "agent_terminal"])
async def test_native_core_failure_redacts_diagnostics_and_never_completes(source):
    from src.agent_runtime.completion import with_completion_gate

    @with_completion_gate
    async def fake_inner(messages, native_terminal_events=False):
        yield 'data: {"delta":"Visible partial"}\n\n'
        if source == "agent_terminal":
            yield 'data: {"type":"agent_terminal","data":{"failure":{"status":429,"message":"SECRET_FAILURE"},"round_texts":["SECRET_TRANSCRIPT"]}}\n\n'
            yield completed_core_event()
        else:
            yield 'event: error\ndata: {"status":503,"error":"SECRET_ENDPOINT"}\n\n'
            yield completed_core_event()

    chunks = [x async for x in fake_inner(
        [{"role": "user", "content": "Explain a queue"}],
        native_terminal_events=True,
    )]
    failures = [item for item in chunks if isinstance(item, CoreFailureEvent)]
    assert len(failures) == 1
    assert failures[0].code == source
    assert failures[0].status == (503 if source == "provider_error" else 429)
    assert not any(isinstance(item, CoreTerminalEvent) for item in chunks)
    assert "SECRET_" not in repr(chunks)


@pytest.mark.asyncio
async def test_native_teacher_gate_preserves_failure_and_prevents_child(monkeypatch):
    import src.teacher_escalation as teacher
    called = []

    async def unexpected_teacher(**_kw):
        called.append(True)
        yield "unexpected"

    monkeypatch.setattr(teacher, "run_teacher_inline", unexpected_teacher)

    @teacher.with_teacher_takeover
    async def failing_student():
        teacher.request_teacher_takeover(owner="alice", session_id="session-1")
        yield CoreTextEvent("message.delta", "Visible fragment")
        yield CoreFailureEvent(code="provider_error", status=502)

    chunks = [x async for x in failing_student()]
    assert len(chunks) == 2
    assert isinstance(chunks[0], CoreTextEvent)
    assert isinstance(chunks[1], CoreFailureEvent)
    assert not called


def test_core_metric_and_failure_factories_keep_only_valid_fields():
    from src.agent_core_events import core_metrics_event, core_failure_event

    metrics = core_metrics_event({
        "input_tokens": True,
        "output_tokens": -42,
        "total_tokens": 8,
        "token": "SECRET",
    })
    assert metrics.input_tokens is None
    assert metrics.output_tokens is None
    assert metrics.total_tokens == 8
    assert not hasattr(metrics, "token")
    assert core_failure_event("provider_error", True).status is None
    assert core_failure_event("provider_error", 502).status == 502
    with pytest.raises(ValueError):
        core_failure_event("private_stack_trace", 500)




@pytest.mark.asyncio
async def test_native_teacher_request_without_done_still_cannot_spawn_child(monkeypatch):
    import src.teacher_escalation as teacher
    called = []

    async def unexpected_teacher(**_kw):
        called.append("child")
        yield "unexpected"

    monkeypatch.setattr(teacher, "run_teacher_inline", unexpected_teacher)

    @teacher.with_teacher_takeover
    async def incomplete_native_student(native_terminal_events=False):
        teacher.request_teacher_takeover(owner="alice", session_id="session-1")
        yield CoreTextEvent("message.delta", "Unfinished answer")

    events = [e async for e in incomplete_native_student(native_terminal_events=True)]
    assert events == [CoreTextEvent("message.delta", "Unfinished answer")]
    assert called == []


def test_all_existing_core_done_emission_sites_offer_native_completion():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "agent_loop.py"
    ).read_text()
    tree = ast.parse(source)
    occurrences = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "completed_core_event"
    ]
    # All eleven original DONE yield sites are covered, no default-frame
    # deletion or changed exact-SSE protocol for existing Web callers.
    assert len(occurrences) == 11
    assert source.count("if native_terminal_events else") == 11


def test_core_event_module_has_no_route_and_no_io_implementation():
    from src import agent_core_events
    source = inspect.getsource(agent_core_events)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("routes", "fastapi", "core.database", "src.tool_execution")
            )
    assert not hasattr(agent_core_events, "router")
