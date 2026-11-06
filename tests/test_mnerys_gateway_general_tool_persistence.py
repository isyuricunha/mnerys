"""Generalized persisted native tool surfaces through the composer.

Extends the original manage_notes persistence slice to any offered native
tool set, including workspace-bound filesystem and process tools. All model
outputs, tool side effects and workspaces are synthetic or disposable.
"""

import os

from core.models import ChatMessage
import pytest

from src import turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_text_persistence import TrustedTextPersistence
from src.gateway_turn_composer import TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_text_persistence import PersistPorts


def general_fixture(monkeypatch, tmp_path=None, *, tools, workspace=False):
    admission, session, _ports, options = configured(monkeypatch)
    ports = PersistPorts()
    writes, partials = [], []

    def write_assistant(current, manager, session_id, text, metrics, **kwargs):
        assert current is session and session_id == session.id
        writes.append((text, metrics))
        current.messages.append({"role": "assistant", "content": text})
        current.history.append(ChatMessage(
            "assistant", text,
            metadata={"_db_id": f"general-assistant-{len(current.history) + 1}"},
        ))

    def write_partial(*args):
        partials.append(args)

    settings = {
        "offered_tools": frozenset(tools),
        "interaction_mode": "agent",
        "persist_verified_native_tools": True,
    }
    if workspace:
        assert tmp_path is not None, "workspace=True requires tmp_path"
        session.cwd = str(tmp_path / "workspace")
        os.makedirs(session.cwd, exist_ok=True)
        settings["workspace"] = session.cwd
    options.update(
        ports=ports,
        settings=TrustedTextTurnSettings(**settings),
        persistence=TrustedTextPersistence(
            object(), write_assistant, interrupted_writer=write_partial,
            emit_message_saved_events=True,
        ),
    )
    return admission, session, ports, options, writes, partials


def tool_turn_model(tools):
    async def model(url, name, messages, **kwargs):
        for tool in tools:
            yield tool_event_frame({
                "type": "tool_start", "tool": tool, "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": tool, "round": 1,
                "exit_code": 0, "output": "PRIVATE_RESULT",
            })
        yield 'data: {"delta":"All actions are done."}\n\n'
        yield "data: [DONE]\n\n"
    return model


@pytest.mark.asyncio
async def test_multi_tool_persisted_turn_saves_the_assistant_once(monkeypatch):
    admission, session, ports, options, writes, partials = general_fixture(
        monkeypatch, tools={"manage_notes", "manage_calendar"},
    )
    seen = []

    async def model(url, name, messages, **kwargs):
        seen.append((kwargs["turn_contract"].offered, kwargs["tool_policy"]))
        async for chunk in tool_turn_model(["manage_notes", "manage_calendar"])(
            url, name, messages, **kwargs,
        ):
            yield chunk

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in stream.events]
    assert seen[0][0] <= frozenset({"manage_notes", "manage_calendar"})
    assert not seen[0][1].blocks("manage_notes")
    assert not seen[0][1].blocks("manage_calendar")
    assert [item.type for item in events] == [
        "tool.started", "tool.completed", "tool.started", "tool.completed",
        "message.delta", "message.saved", "message.final", "turn.completed",
    ]
    assert [item.sequence for item in events] == list(range(1, len(events) + 1))
    assert writes == [("All actions are done.", None)]
    assert len(session.messages) == 2
    assert ports.operations.count("save_user") == 1
    assert stream.outcome.state == "completed"
    assert not partials
    assert "PRIVATE_RESULT" not in repr(events)


@pytest.mark.asyncio
async def test_workspace_filesystem_tool_persists_with_bound_workspace(
    monkeypatch, tmp_path,
):
    admission, session, _ports, options, writes, _partials = general_fixture(
        monkeypatch, tmp_path, tools={"read_file"}, workspace=True,
    )
    seen = {}

    async def model(url, name, messages, **kwargs):
        seen["workspace"] = kwargs["workspace"]
        seen["contract"] = kwargs["turn_contract"]
        seen["policy"] = kwargs["tool_policy"]
        async for chunk in tool_turn_model(["read_file"])(url, name, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    [item async for item in stream.events]
    assert seen["workspace"] == session.cwd
    assert "read_file" in seen["contract"].executable
    assert not seen["policy"].blocks("read_file")
    assert writes == [("All actions are done.", None)]
    assert len(session.messages) == 2
    assert stream.outcome.state == "completed"
    assert not _partials


@pytest.mark.asyncio
async def test_process_tool_persisted_turn_keeps_original_execution_order(
    monkeypatch,
):
    admission, session, _ports, options, writes, _partials = general_fixture(
        monkeypatch, tools={"bash"},
    )
    order = []

    async def model(url, name, messages, **kwargs):
        order.append("tool")
        async for chunk in tool_turn_model(["bash"])(url, name, messages, **kwargs):
            yield chunk
        order.append("final")

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in stream.events]
    assert order == ["tool"]
    assert events[-1].type == "turn.completed"
    assert writes == [("All actions are done.", None)]


@pytest.mark.asyncio
async def test_general_tool_review_pause_does_not_save_an_assistant(monkeypatch):
    admission, session, ports, options, writes, partials = general_fixture(
        monkeypatch, tools={"manage_calendar"},
    )
    store = ToolApprovalStore()
    options["approval_store"] = store
    pending_ids = []

    async def model(url, name, messages, **kwargs):
        action = '{"action":"list"}'
        pending = store.create(
            owner="alice", session_id=session.id, origin_run_id="general-test-run",
            tool_name="manage_calendar", content=action, workspace=None,
            request_text="What is on my calendar",
            continuation_query="What is on my calendar",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_calendar", action),
            selected_tools={"manage_calendar"},
            request_authority=kwargs["request_authority"],
        )
        pending_ids.append(pending.approval_id)
        yield approval_requested_event(pending)
        yield tool_event_frame(
            {
                "type": "tool_output", "tool": "manage_calendar",
                "round": 1, "exit_code": None, "output": "PRIVATE_REVIEW",
            },
            result={"ask_user": pending.public_payload()},
        )
        yield 'data: {"type":"final_response","content":"Approval required."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in stream.events]
    assert [item.type for item in events] == [
        "approval.requested", "tool.awaiting_approval", "turn.awaiting_approval",
    ]
    assert stream.outcome.state == "awaiting_approval"
    assert store.peek(pending_ids[0]) is not None
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "user"
    assert not writes and not partials
