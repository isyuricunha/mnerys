"""M2.1 native Agent Core completion signal, with unchanged default Web SSE.

The real inherited Agent Loop executes an isolated, model-free missing-
workspace early exit; both output formats use the same internal path.
"""

import ast
import inspect
from pathlib import Path

import pytest

from src.agent_core_events import (
    CoreTerminalEvent,
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
    assert all(isinstance(item, str) for item in outputs[:-1])
    assert any("No active workspace is set" in item for item in outputs)
    assert any('"missing_workspace": true' in item for item in outputs)
    if native:
        assert isinstance(outputs[-1], CoreTerminalEvent)
        assert outputs[-1].type == "turn.completed"
    else:
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
        "message.delta", "message.final", "turn.completed",
    ]
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
