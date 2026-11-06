"""Opt-in native notes persistence reuses the admitted writer and approval gate.

This does not expose a new HTTP tool profile or grant filesystem access.
All model outputs and tool side effects in this suite are synthetic.
"""

from dataclasses import replace

from core.models import ChatMessage
import pytest

from src import turn_service as runtime
from src.agent_runtime.authority import RequestAuthority
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_admission import AdmissionDenied
from src.gateway_text_persistence import TrustedTextPersistence
from src.gateway_turn_composer import (
    TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings,
)
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_text_persistence import PersistPorts


def native_fixture(monkeypatch):
    admission, session, _ports, options = configured(monkeypatch)
    ports = PersistPorts()
    writes, partials = [], []

    def write_assistant(current, manager, session_id, text, metrics, **kwargs):
        assert current is session and session_id == session.id
        assert manager is session_manager
        writes.append((text, metrics))
        current.messages.append({"role": "assistant", "content": text})
        current.history.append(ChatMessage(
            "assistant", text,
            metadata={"_db_id": f"native-assistant-{len(current.history) + 1}"},
        ))

    def write_partial(*args):
        partials.append(args)

    session_manager = object()
    options.update(
        ports=ports,
        settings=replace(
            options["settings"],
            persist_verified_native_tools=True,
        ),
        persistence=TrustedTextPersistence(
            session_manager, write_assistant,
            interrupted_writer=write_partial,
            emit_message_saved_events=True,
        ),
    )
    return admission, session, ports, options, writes, partials


@pytest.mark.asyncio
async def test_native_tool_turn_saves_after_original_loop_and_writer(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    execution = []

    async def model(url, name, messages, **kwargs):
        assert kwargs["request_authority"].bound_to(
            owner="alice", session_id=session.id,
        )
        assert kwargs["turn_contract"].offered == frozenset({"manage_notes"})
        assert kwargs["tool_policy"].blocks("bash")
        assert not kwargs["tool_policy"].blocks("manage_notes")
        execution.append(True)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes",
            "round": 1, "command": "PRIVATE_NOTE_QUERY",
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0, "output": "PRIVATE_NOTE_RESULT",
        })
        yield 'data: {"delta":"Three notes found."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in stream.events]
    assert execution == [True]
    assert [item.type for item in events] == [
        "tool.started", "tool.completed", "message.delta",
        "message.saved", "message.final", "turn.completed",
    ]
    assert [item.sequence for item in events] == list(range(1, len(events) + 1))
    assert writes == [("Three notes found.", None)]
    assert len(session.messages) == 2
    assert session.messages[0]["role"] == "user"
    assert session.messages[1]["role"] == "assistant"
    assert ports.operations.count("save_user") == 1
    assert stream.outcome.state == "completed"
    assert not partials
    assert "PRIVATE_NOTE" not in repr(events)


@pytest.mark.asyncio
async def test_tool_response_writer_failure_is_never_acknowledged_or_retried(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    attempts = []

    def failing_saver(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("private database diagnostic")

    options["persistence"] = replace(
        options["persistence"], saver=failing_saver,
    )

    async def model(*args, **kwargs):
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Visible result"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in delivery.events]
    assert [item.type for item in events] == [
        "tool.started", "tool.completed", "message.delta", "turn.failed",
    ]
    assert [item.sequence for item in events] == [1, 2, 3, 4]
    assert events[-1].payload == {
        "code": "persistence_error", "status": None,
    }
    assert attempts == [True]
    assert len(session.messages) == 1
    assert not writes and not partials
    assert delivery.outcome.state == "failed"
    assert "private database" not in repr(events)


@pytest.mark.asyncio
async def test_persisted_native_notes_account_denial_precedes_user_write(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    options["settings"] = replace(
        options["settings"], privileges={"can_use_agent": False},
    )
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    with pytest.raises(AdmissionDenied):
        _ = [item async for item in delivery.events]
    assert ports.operations == []
    assert session.messages == []
    assert not writes and not partials


@pytest.mark.asyncio
async def test_pending_native_review_is_not_a_saved_assistant_or_partial(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    store = ToolApprovalStore()
    options["approval_store"] = store
    pending_ids = []

    async def model(url, name, messages, **kwargs):
        action = '{"action":"list"}'
        pending = store.create(
            owner="alice", session_id=session.id, origin_run_id="native-test-run",
            tool_name="manage_notes", content=action, workspace=None,
            request_text="List my notes",
            continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", action),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending_ids.append(pending.approval_id)
        yield approval_requested_event(pending)
        yield tool_event_frame(
            {
                "type": "tool_output", "tool": "manage_notes",
                "round": 1, "exit_code": None, "output": "PRIVATE_REVIEW",
            },
            result={"ask_user": pending.public_payload()},
        )
        yield 'data: {"type":"final_response","content":"Approval required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in delivery.events]
    assert [item.type for item in events] == [
        "approval.requested", "tool.awaiting_approval", "turn.awaiting_approval",
    ]
    assert [item.sequence for item in events] == [1, 2, 3]
    assert events[-1].payload == {"approval_id": pending_ids[0]}
    assert delivery.outcome.state == "awaiting_approval"
    assert store.peek(pending_ids[0]) is not None
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "user"
    assert ports.operations.count("save_user") == 1
    assert not writes and not partials


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "no_optin", "no_tools", "no_persistence",
    "bad_flag", "chat_mode", "wrong_workspace",
    "attachment", "continuation",
])
async def test_invalid_persisted_native_tool_profile_has_no_context_io(monkeypatch, invalid):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    current = options["settings"]
    if invalid == "no_optin":
        options["settings"] = replace(current, persist_verified_native_tools=False)
    elif invalid == "no_tools":
        options["settings"] = replace(current, offered_tools=frozenset())
    elif invalid == "no_persistence":
        options["persistence"] = None
    elif invalid == "bad_flag":
        options["settings"] = replace(current, persist_verified_native_tools="true")
    elif invalid == "chat_mode":
        options["settings"] = replace(current, interaction_mode="chat")
    elif invalid == "wrong_workspace":
        # Any offered native tool set may persist now, but the selected
        # workspace must still be the admitted session's own workspace.
        options["settings"] = replace(current, workspace="/example")
    elif invalid == "attachment":
        options["request"] = TextTurnRequest("List my notes", ("note.md",))
    else:
        options["request"] = ToolApprovalTurnRequest("opaque", "approve")

    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    with pytest.raises(AdmissionDenied):
        _ = [item async for item in delivery.events]
    assert not ports.operations
    assert session.messages == []
    assert not writes and not partials


@pytest.mark.asyncio
async def test_inherited_agent_loop_executes_once_before_persisted_completion(monkeypatch):
    """Exercise the actual Loop and dispatcher bridge, not synthetic tool frames."""
    import json

    from src import agent_loop as loop
    from src.agent_runtime.authority import ExactOperation, active_request_authority
    from src.turn_contract import active_turn_contract
    from tests.test_required_read_operation import notes_result

    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *args, **kwargs: 10)

    effects = []

    async def provider(*args, **kwargs):
        yield 'data: {"delta":"Your notes are ready."}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(block, **kwargs):
        assert session.history[0].role == "user"
        assert len(session.history) == 1
        assert active_request_authority().bound_to(
            owner="alice", session_id=session.id,
        )
        assert active_request_authority().permits(
            ExactOperation.normalize(block.tool_type, block.content),
        )
        assert not active_request_authority().permits(
            ExactOperation.normalize("manage_notes", '{"action":"add","title":"unapproved"}'),
        )
        assert active_turn_contract().permits(block.tool_type)
        assert not kwargs["tool_policy"].blocks(block.tool_type)
        assert json.loads(block.content)["action"] == "list"
        effects.append(block.tool_type)
        return "manage_notes: list", notes_result()

    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(loop, "execute_tool_block", execute)
    gateway = GatewayTurns(TurnService())
    delivery = gateway.open_composed_text_events(admission, session, **options)
    events = [item async for item in delivery.events]

    assert effects == ["manage_notes"]
    assert [item.type for item in events].count("tool.started") == 1
    assert [item.type for item in events].count("tool.completed") == 1
    assert [item.type for item in events].count("message.saved") == 1
    assert events[-1].type == "turn.completed"
    assert len(writes) == 1
    assert not partials
    assert ports.operations.count("save_user") == 1
