"""Persisted native review: one original user receipt, one approval effect.

This is a direct, process-local Gateway acceptance test with synthetic model
and tool observations; it is not remote HTTP or a durable approval database.
"""

from dataclasses import replace

from core.models import ChatMessage
import pytest

from src import turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import ToolApprovalTurnRequest
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_native_tool_persistence import native_fixture


async def paused_native_review(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    store = ToolApprovalStore()
    options["approval_store"] = store
    pending = []

    async def request_review(_url, _name, _messages, **kwargs):
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="persisted-native-review-fixture",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes",
            continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(record)
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": record.public_payload()})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", request_review)
    gateway = GatewayTurns(TurnService())
    delivery = gateway.open_composed_text_events(admission, session, **options)
    events = [event async for event in delivery.events]
    assert [event.type for event in events] == [
        "approval.requested", "tool.awaiting_approval", "turn.awaiting_approval",
    ]
    assert delivery.outcome.state == "awaiting_approval"
    assert store.peek(pending[0].approval_id) is pending[0]
    assert len(session.history) == 1
    assert session.history[0].metadata["_db_id"]
    assert ports.operations.count("save_user") == 1
    assert not writes and not partials
    return gateway, admission, session, ports, options, store, pending[0], writes, partials


@pytest.mark.asyncio
async def test_persisted_native_review_approved_once_with_single_original_user(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )
    calls = []

    async def continued(_url, _name, messages, **kwargs):
        assert kwargs["exact_approval"].pending is pending
        assert sum(item.get("content") == "List my notes" for item in messages) == 1
        assert kwargs["exact_approval"].claim(
            owner="alice", session_id=session.id,
            tool_name="manage_notes", content=pending.content, workspace=None,
        )
        calls.append("one approved effect")
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Approved notes response."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", continued)
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")
    delivery = gateway.open_composed_text_events(admission, session, **options)
    events = [event async for event in delivery.events]
    assert [event.type for event in events] == [
        "approval.decided", "tool.started", "tool.completed", "message.delta",
        "message.saved", "message.final", "turn.completed",
    ]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert delivery.outcome.state == "completed"
    assert calls == ["one approved effect"]
    assert ports.operations.count("save_user") == 1
    assert [message.role for message in session.history] == ["user", "assistant"]
    assert writes == [("Approved notes response.", None)]
    assert not partials
    assert store.peek(pending.approval_id) is None
    assert "PRIVATE" not in repr(events)

    with pytest.raises(AdmissionDenied):
        replay = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in replay.events]
    assert calls == ["one approved effect"]


@pytest.mark.asyncio
async def test_persisted_native_denial_consumes_without_second_user_or_model(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied action invoked the model")
        yield

    monkeypatch.setattr(runtime, "stream_agent_loop", forbidden)
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "deny")
    delivery = gateway.open_composed_text_events(admission, session, **options)
    events = [event async for event in delivery.events]
    assert [event.type for event in events] == [
        "approval.decided", "message.saved", "message.final", "turn.completed",
    ]
    assert writes == [("The action was denied.", None)]
    assert [message.role for message in session.history] == ["user", "assistant"]
    assert ports.operations.count("save_user") == 1
    assert store.peek(pending.approval_id) is None
    assert not partials


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "missing_receipt", "wrong_request", "wrong_role", "newer_message",
    "duplicate_receipt", "missing_history",
])
async def test_persisted_review_rejects_unbound_original_before_consuming(
    monkeypatch, change,
):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )
    initial_ops = list(ports.operations)
    initial_messages = list(session.messages)
    origin = session.history[0]
    if change == "missing_receipt":
        origin.metadata.pop("_db_id")
    elif change == "wrong_request":
        origin.content = "different request"
    elif change == "wrong_role":
        origin.role = "assistant"
    elif change == "newer_message":
        session.history.append(ChatMessage(
            "assistant", "unexpected", metadata={"_db_id": "newer-record"},
        ))
    elif change == "duplicate_receipt":
        session.history.insert(0, ChatMessage(
            "user", "old copy", metadata={"_db_id": origin.metadata["_db_id"]},
        ))
    else:
        session.history.clear()

    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve")
    with pytest.raises(AdmissionDenied):
        delivery = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in delivery.events]
    assert store.peek(pending.approval_id) is pending
    assert ports.operations == initial_ops
    assert session.messages == initial_messages
    assert not writes and not partials


@pytest.mark.asyncio
async def test_persisted_review_real_agent_loop_dispatches_exact_notes_effect_once(monkeypatch):
    """The actual inherited Loop claims one sealed effect, not a fake frame."""
    import json

    from src import agent_loop as loop
    from src.agent_runtime.authority import RequestAuthority
    import src.tool_implementations as implementations
    import src.tool_index as tool_index
    from src.tool_approvals import active_tool_approval_store, tool_approval_store
    from src.gateway_turn_composer import TextTurnRequest

    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    original_text = "Add a note titled Example with content Hello."
    options["request"] = TextTurnRequest(original_text)
    store = ToolApprovalStore()
    options["approval_store"] = store
    # The inherited approval gate is triggered only after untrusted context.
    # Retire a prior sealed review, as the real Loop does on a new user turn.
    old_content = '{"action":"list"}'
    stale = store.create(
        owner="alice", session_id=session.id, origin_run_id="previous-turn",
        tool_name="manage_notes", content=old_content, workspace=None,
        request_text="Earlier request", continuation_query="Earlier request",
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("manage_notes", old_content),
        selected_tools={"manage_notes"},
        request_authority=RequestAuthority.empty(owner="alice", session_id=session.id),
    )
    monkeypatch.setattr(loop, "get_setting", lambda _key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda _owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *_a, **_k: 10)
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)

    content = json.dumps({"action": "add", "title": "Example", "content": "Hello"})
    effects = []

    async def notes(action, **_kwargs):
        assert active_tool_approval_store(tool_approval_store) is store
        assert json.loads(action) == json.loads(content)
        effects.append(action)
        return {"output": "Example note created.", "exit_code": 0, "note_id": "example"}

    async def provider(*_args, **_kwargs):
        yield 'data: ' + json.dumps({
            "type": "tool_calls",
            "calls": [{"id": "persisted-review-call", "name": "manage_notes",
                       "arguments": content}],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(implementations, "do_manage_notes", notes)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    gateway = GatewayTurns(TurnService())
    first = gateway.open_composed_text_events(admission, session, **options)
    first_events = [event async for event in first.events]
    assert first.outcome.state == "awaiting_approval"
    assert store.peek(stale.approval_id) is None
    assert not effects and not writes
    assert ports.operations.count("save_user") == 1
    pending_id = next(
        event.payload["approval_id"] for event in first_events
        if event.type == "approval.requested"
    )
    pending = store.peek(pending_id)
    assert pending is not None
    assert pending.request_text == original_text
    assert session.history[-1].metadata["_db_id"]
    options["request"] = ToolApprovalTurnRequest(pending_id, "approve_task")
    second = gateway.open_composed_text_events(admission, session, **options)
    second_events = [event async for event in second.events]
    assert second.outcome.state == "completed"
    assert len(effects) == 1 and json.loads(effects[0]) == json.loads(content)
    assert store.peek(pending_id) is None
    assert ports.operations.count("save_user") == 1
    assert [message.role for message in session.history] == ["user", "assistant"]
    assert len(writes) == 1
    assert not partials
    assert [event.type for event in second_events].count("tool.completed") == 1
    assert second_events[-1].type == "turn.completed"
    assert active_tool_approval_store(tool_approval_store) is tool_approval_store
    with pytest.raises(AdmissionDenied):
        replay = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in replay.events]
    assert len(effects) == 1 and json.loads(effects[0]) == json.loads(content)


@pytest.mark.asyncio
async def test_persisted_review_origin_rechecked_after_async_preprocessing(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )
    previous = ports.preprocess_message

    async def changed_while_preparing(*args, **kwargs):
        prepared = await previous(*args, **kwargs)
        # Simulate a different session writer committing between the first
        # receipt verification and the one-use approval consumption.
        session.history.append(ChatMessage(
            "user", "A newer question", metadata={"_db_id": "new-user-receipt"},
        ))
        return prepared

    monkeypatch.setattr(ports, "preprocess_message", changed_while_preparing)
    before = ports.operations.count("save_user")
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")
    with pytest.raises(AdmissionDenied):
        delivery = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in delivery.events]
    assert store.peek(pending.approval_id) is pending
    assert ports.operations.count("save_user") == before
    assert not writes and not partials


@pytest.mark.asyncio
async def test_persisted_review_writer_failure_does_not_reclaim_approved_effect(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )
    effects, saves = [], []

    async def approved(_url, _model, _messages, **kwargs):
        assert kwargs["exact_approval"].claim(
            owner="alice", session_id=session.id, tool_name="manage_notes",
            content=pending.content, workspace=None,
        )
        effects.append(True)
        yield 'data: {"delta":"Effect was completed"}\n\n'
        yield "data: [DONE]\n\n"

    def failed_writer(*_args, **_kwargs):
        saves.append(True)
        raise RuntimeError("private writer detail")

    monkeypatch.setattr(runtime, "stream_agent_loop", approved)
    options["persistence"] = replace(options["persistence"], saver=failed_writer)
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve")
    delivery = gateway.open_composed_text_events(admission, session, **options)
    events = [event async for event in delivery.events]
    assert events[-1].type == "turn.failed"
    assert events[-1].payload == {"code": "persistence_error", "status": None}
    assert [event.type for event in events] == [
        "approval.decided", "message.delta", "turn.failed",
    ]
    assert effects == saves == [True]
    assert store.peek(pending.approval_id) is None
    assert ports.operations.count("save_user") == 1
    assert [message.role for message in session.history] == ["user"]
    assert not writes and not partials
    assert "private writer detail" not in repr(events)

    with pytest.raises(AdmissionDenied):
        retry = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in retry.events]
    assert effects == saves == [True]


@pytest.mark.asyncio
async def test_persisted_native_approval_rechecks_current_tool_policy(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await paused_native_review(monkeypatch)
    )
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve")
    options["settings"] = replace(
        options["settings"], global_disabled_tools=("manage_notes",),
    )
    with pytest.raises(AdmissionDenied):
        delivery = gateway.open_composed_text_events(admission, session, **options)
        _ = [event async for event in delivery.events]
    assert store.peek(pending.approval_id) is pending
    assert ports.operations.count("save_user") == 1
    assert not writes and not partials
