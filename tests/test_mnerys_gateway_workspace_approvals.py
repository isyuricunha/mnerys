"""M2.1 session-bound workspace approvals via one inherited Agent Loop.

The tests use isolated temporary directories and synthetic model responses.
No filesystem tool effects, production workspaces or UI documents are opened.
"""

from dataclasses import replace
import asyncio
import json

import pytest

from src.agent_runtime.authority import RequestAuthority, ExactOperation
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from src.tool_approvals import ExactToolApproval
from tests.test_mnerys_gateway_composed_approvals import setup
from tests.test_mnerys_gateway_composed_tools import configured


def workspace_pending(monkeypatch, tmp_path):
    workspace = str(tmp_path / "session-workspace")
    (tmp_path / "session-workspace").mkdir()
    original = RequestAuthority.empty(
        owner="alice", session_id="session-1", workspace=workspace,
    )
    admission, session, ports, options, store, pending = setup(
        monkeypatch, workspace=workspace, request_authority=original,
    )
    session.cwd = workspace
    options["settings"] = replace(options["settings"], workspace=workspace)
    return workspace, admission, session, ports, options, store, pending


async def collect(admission, session, options):
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [event async for event in delivery.events]
    return delivery, events


@pytest.mark.asyncio
async def test_workspace_native_approval_uses_existing_sealed_scope(monkeypatch, tmp_path):
    import src.turn_service as agent
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    seen = []

    async def loop(url, model, messages, **kw):
        seen.append(kw)
        assert kw["workspace"] == workspace
        assert kw["request_authority"].bound_to(
            owner="alice", session_id=session.id, workspace=workspace,
        )
        assert kw["request_authority"].workspace == pending.request_authority.workspace
        assert kw["request_authority"].grants == ()
        assert kw["turn_contract"].offered == frozenset({"manage_notes"})
        exact = kw["exact_approval"]
        assert isinstance(exact, ExactToolApproval)
        assert exact.pending is pending
        assert exact.claim(
            owner="alice", session_id=session.id, workspace=workspace,
            tool_name=pending.tool_name, content=pending.content,
        )
        assert not exact.claim(
            owner="alice", session_id=session.id, workspace=workspace,
            tool_name=pending.tool_name, content=pending.content,
        )
        assert messages[-1]["content"] == pending.continuation_query
        yield 'data: {"delta":"Notes listed."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent, "stream_agent_loop", loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert seen and len(seen) == 1
    assert events[0].type == "approval.decided"
    assert events[-1].type == "turn.completed"
    assert store.peek(pending.approval_id) is None
    assert not session.messages and ports.operations
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)


@pytest.mark.asyncio
async def test_workspace_bound_ordinary_turn_reuses_policy_and_contract(monkeypatch, tmp_path):
    import src.turn_service as agent
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    admission, session, ports, options = configured(monkeypatch)
    session.cwd = workspace
    options["settings"] = replace(options["settings"], workspace=workspace)
    options["request"] = TextTurnRequest("List my notes")
    seen = []

    async def loop(url, model, messages, **kw):
        seen.append(kw)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent, "stream_agent_loop", loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert seen[0]["workspace"] == workspace
    assert seen[0]["request_authority"].bound_to(
        owner="alice", session_id=session.id, workspace=workspace,
    )
    assert seen[0]["turn_contract"].offered == frozenset({"manage_notes"})
    assert seen[0]["tool_policy"].blocks("bash")
    assert seen[0]["tool_policy"].disable_mcp
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "missing_adapter_workspace",
    "unbound_session_cwd",
    "different_session_cwd",
    "wrong_trusted_workspace",
    "missing_directory",
    "relative_workspace",
    "untrusted_control_char",
    "inconsistent_original_authority",
])
async def test_invalid_workspace_approval_keeps_card_unconsumed(
    monkeypatch, tmp_path, scenario,
):
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    second = str(tmp_path / "other-workspace")
    (tmp_path / "other-workspace").mkdir()
    if scenario == "missing_adapter_workspace":
        options["settings"] = replace(options["settings"], workspace=None)
    elif scenario == "unbound_session_cwd":
        session.cwd = None
    elif scenario == "different_session_cwd":
        session.cwd = second
    elif scenario == "wrong_trusted_workspace":
        options["settings"] = replace(options["settings"], workspace=second)
    elif scenario == "missing_directory":
        session.cwd = str(tmp_path / "missing")
        options["settings"] = replace(options["settings"], workspace=session.cwd)
    elif scenario == "relative_workspace":
        session.cwd = "relative"
        options["settings"] = replace(options["settings"], workspace="relative")
    elif scenario == "untrusted_control_char":
        options["settings"] = replace(options["settings"], workspace=workspace + "\n")
    elif scenario == "inconsistent_original_authority":
        original = replace(
            pending.request_authority, workspace=second,
        )
        object.__setattr__(pending, "request_authority", original)

    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations and not session.messages


@pytest.mark.asyncio
async def test_workspace_changed_during_preprocess_cannot_consume(monkeypatch, tmp_path):
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    alternative = str(tmp_path / "other")
    (tmp_path / "other").mkdir()
    original = ports.preprocess_message

    async def mutate(*args, **kw):
        result = await original(*args, **kw)
        session.cwd = alternative
        return result

    monkeypatch.setattr(ports, "preprocess_message", mutate)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not session.messages


@pytest.mark.asyncio
async def test_workspace_does_not_enable_persisting_tool_turn(monkeypatch, tmp_path):
    from src.gateway_text_persistence import TrustedTextPersistence
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    options["persistence"] = TrustedTextPersistence(
        object(), lambda *args, **kwargs: None,
    )
    with pytest.raises(AdmissionDenied, match="plain text"):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations


@pytest.mark.asyncio
async def test_document_bound_approval_still_unsupported_with_workspace(
    monkeypatch, tmp_path,
):
    workspace = str(tmp_path / "approved")
    (tmp_path / "approved").mkdir()
    original = RequestAuthority.empty(
        owner="alice", session_id="session-1", workspace=workspace,
    )
    admission, session, ports, options, store, pending = setup(
        monkeypatch, workspace=workspace, request_authority=original,
        document_id="draft", document_version=1,
    )
    session.cwd = workspace
    options["settings"] = replace(options["settings"], workspace=workspace)
    with pytest.raises(AdmissionDenied, match="unsupported"):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations


@pytest.mark.asyncio
async def test_workspace_context_failure_or_cancel_preserves_approval(
    monkeypatch, tmp_path,
):
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    entered = asyncio.Event()

    async def cancel(*args, **kw):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ports, "preprocess_message", cancel)
    task = asyncio.create_task(collect(admission, session, options))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
async def test_actual_agent_loop_workspace_approval_executes_one_sealed_native_effect(
    monkeypatch, tmp_path,
):
    import src.agent_loop as loop
    import src.tool_implementations as actions
    from src.agent_runtime.authority import active_request_authority
    from src.tool_approvals import (
        active_tool_approval_store, tool_approval_store,
    )
    from src.gateway_turn_composer import ToolApprovalTurnRequest

    workspace, admission, session, ports, options, store, retired = workspace_pending(
        monkeypatch, tmp_path,
    )
    options["request"] = TextTurnRequest("Add a note titled Example with content Hello.")
    command = json.dumps({
        "action": "add", "title": "Example", "content": "Hello",
    })
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *args, **kw: 10)
    effects = []

    async def notes(payload, **kw):
        assert active_tool_approval_store(tool_approval_store) is store
        assert active_request_authority().bound_to(
            owner="alice", session_id=session.id, workspace=workspace,
        )
        assert json.loads(payload) == json.loads(command)
        effects.append(payload)
        return {
            "output": "Example note created.",
            "exit_code": 0,
            "note_id": "example-note",
        }

    async def model(*args, **kw):
        yield 'data: ' + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "example-call",
                "name": "manage_notes",
                "arguments": command,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(actions, "do_manage_notes", notes)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", model)
    requested_delivery, request_events = await collect(admission, session, options)
    assert requested_delivery.outcome.state == "awaiting_approval"
    assert request_events[-1].type == "turn.awaiting_approval"
    assert effects == []
    assert store.peek(retired.approval_id) is None
    proposal = next(e for e in request_events if e.type == "approval.requested")
    pending = store.peek(proposal.payload["approval_id"])
    assert pending is not None
    assert pending.workspace == workspace
    assert pending.request_authority.workspace == workspace
    assert json.loads(pending.content) == json.loads(command)

    options["request"] = ToolApprovalTurnRequest(
        pending.approval_id, "approve_task",
    )
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert events[0].type == "approval.decided"
    assert any(e.type == "tool.completed" for e in events)
    assert len(effects) == 1
    assert store.peek(pending.approval_id) is None
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert len(effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["account", "global", "surface"])
async def test_workspace_approval_cannot_bypass_live_tool_policy(
    monkeypatch, tmp_path, blocked,
):
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    settings = options["settings"]
    if blocked == "account":
        settings = replace(settings, privileges={"can_use_agent": False})
    elif blocked == "global":
        settings = replace(settings, global_disabled_tools=("manage_notes",))
    else:
        settings = replace(settings, offered_tools=frozenset())
    options["settings"] = settings

    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not session.messages and not ports.operations


@pytest.mark.asyncio
async def test_workspace_symlink_retarget_during_preprocess_cannot_consume(
    monkeypatch, tmp_path,
):
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    alias = tmp_path / "active-workspace"
    alias.symlink_to(workspace, target_is_directory=True)
    other = tmp_path / "new-target"
    other.mkdir()
    session.cwd = str(alias)
    options["settings"] = replace(options["settings"], workspace=str(alias))
    original = ports.preprocess_message

    async def retarget(*args, **kw):
        result = await original(*args, **kw)
        # Only mutate the symlink created inside this disposable test folder.
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        return result

    monkeypatch.setattr(ports, "preprocess_message", retarget)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["missing_cwd", "changed_cwd", "invalid_adapter_path"])
async def test_denial_can_retire_workspace_card_even_after_context_is_lost(
    monkeypatch, tmp_path, context,
):
    from src.gateway_turn_composer import ToolApprovalTurnRequest
    workspace, admission, session, ports, options, store, pending = workspace_pending(
        monkeypatch, tmp_path,
    )
    if context == "missing_cwd":
        session.cwd = None
    elif context == "changed_cwd":
        other = tmp_path / "other"
        other.mkdir()
        session.cwd = str(other)
    else:
        options["settings"] = replace(
            options["settings"], workspace="relative-untrusted",
        )
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "deny")
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert [event.type for event in events] == [
        "approval.decided", "message.final", "turn.completed",
    ]
    assert store.peek(pending.approval_id) is None
    assert not ports.operations and not session.messages
