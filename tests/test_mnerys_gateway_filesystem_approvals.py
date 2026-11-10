"""Direct filesystem approval resumes only a previously sealed workspace target.

All tool effects operate on pytest temporary files; no production files,
configuration, network services, or shell commands are used.
"""

import asyncio
from dataclasses import replace
import json

import pytest

from src.agent_runtime.authority import RequestAuthority
from src.agent_runtime.resource_binding import NATIVE_FILESYSTEM_TOOLS
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import ToolApprovalTurnRequest, TextTurnRequest
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured


def sealed_turn(monkeypatch, tmp_path, *, tool="read_file", content="a.txt"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("Original text.", encoding="utf-8")
    (workspace / "b.txt").write_text("Different text.", encoding="utf-8")
    original = RequestAuthority.empty(
        owner="alice", session_id="session-1", workspace=str(workspace),
    )
    admission, session, ports, options = configured(monkeypatch)
    session.cwd = str(workspace)
    options["settings"] = replace(
        options["settings"],
        workspace=str(workspace),
        offered_tools=frozenset({tool}),
    )
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id=session.id,
        origin_run_id="fs-fixture-run", tool_name=tool, content=content,
        workspace=str(workspace), external_untrusted_context_seen=True,
        capabilities=capabilities_for_action(tool, content),
        selected_tools={tool}, continuation_query="Use the approved filesystem path.",
        request_text="Use the approved filesystem path.",
        request_authority=original,
    )
    options["approval_store"] = store
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")
    return workspace, admission, session, ports, options, store, pending


async def collect(admission, session, options):
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [event async for event in delivery.events]
    return delivery, events


@pytest.mark.asyncio
async def test_sealed_read_file_executes_once_via_actual_agent_loop(monkeypatch, tmp_path):
    import src.agent_loop as loop
    from src.tool_approvals import active_tool_approval_store, tool_approval_store

    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
    )
    assert pending.resource_operation is not None
    assert pending.request_authority.resource_roots == ()
    assert pending.resource_operation.bindings[0].resource.path == str(workspace / "a.txt")
    assert pending.resource_operation.bindings[0].resource.root.owner == "alice"

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    # Filesystem tools are admin-restricted on public deployments. This
    # fixture explicitly simulates the existing authorized admin path.
    import src.tool_execution as execution
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: owner == "alice")

    requests = []
    inspected = []
    original_execute = loop.execute_tool_block

    async def inspect_execution(block, **kwargs):
        from src.turn_contract import active_turn_contract
        contract = active_turn_contract()
        inspected.append((block.tool_type, contract.offered, contract.unavailable,
                         contract.capabilities, contract.required, contract.permits(block.tool_type)))
        return await original_execute(block, **kwargs)

    monkeypatch.setattr(loop, "execute_tool_block", inspect_execution)

    async def provider(*args, **kwargs):
        requests.append(True)
        assert active_tool_approval_store(tool_approval_store) is store
        from src.turn_contract import active_turn_contract
        contract = active_turn_contract()
        assert contract.permits(pending.tool_name), (contract.offered, contract.unavailable, contract.capabilities)
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "exact-read-file",
                "name": pending.tool_name,
                "arguments": pending.content,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert events[0].type == "approval.decided"
    assert any(event.type == "tool.completed" for event in events), (inspected, [(event.type, event.payload) for event in events])
    assert requests == [True]
    assert store.peek(pending.approval_id) is None
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "Original text."
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "Different text."
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert requests == [True]


@pytest.mark.asyncio
async def test_sealed_write_file_has_exact_single_destination(monkeypatch, tmp_path):
    import src.agent_loop as loop
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path, tool="write_file",
        content="created.txt\nApproved content.",
    )
    assert pending.resource_operation is not None
    assert pending.resource_operation.bindings[0].resource.identity is None
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    import src.tool_execution as execution
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: owner == "alice")

    inspected = []
    original_execute = loop.execute_tool_block

    async def inspect_execution(block, **kwargs):
        from src.turn_contract import active_turn_contract
        contract = active_turn_contract()
        inspected.append((block.tool_type, contract.offered, contract.unavailable,
                         contract.capabilities, contract.required, contract.permits(block.tool_type)))
        return await original_execute(block, **kwargs)

    monkeypatch.setattr(loop, "execute_tool_block", inspect_execution)

    async def provider(*args, **kwargs):
        from src.turn_contract import active_turn_contract
        contract = active_turn_contract()
        assert contract.permits(pending.tool_name), (contract.offered, contract.unavailable, contract.capabilities)
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "exact-write-file",
                "name": pending.tool_name,
                "arguments": pending.content,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert any(event.type == "tool.completed" for event in events), (inspected, [(event.type, event.payload) for event in events])
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "Approved content."
    assert store.peek(pending.approval_id) is None
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert sorted(item.name for item in workspace.iterdir()) == [
        "a.txt", "b.txt", "created.txt",
    ]


@pytest.mark.asyncio
async def test_sealed_edit_file_requires_recorded_read_and_executes_once(monkeypatch, tmp_path):
    """M5.2 read-before-edit: the approved edit of an EXISTING file executes
    exactly once when the session recorded the read, and the sealed replay of
    the pending identity stays a legitimate internal handler-identifier flow."""
    import src.agent_loop as loop
    from src.agent_runtime.read_baselines import record_file_read
    content = json.dumps({
        "path": "a.txt",
        "old_string": "Original text.",
        "new_string": "Approved edit.",
    })
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path, tool="edit_file", content=content,
    )
    # The realistic approval flow: the model read the file in this session
    # before proposing the edit, so the server-side baseline exists.
    record_file_read("alice", session.id, str(workspace / "a.txt"), ("full", 1, 0))
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    import src.tool_execution as execution
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: owner == "alice")

    async def provider(*args, **kwargs):
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "exact-edit-file",
                "name": pending.tool_name,
                "arguments": pending.content,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert any(event.type == "tool.completed" for event in events), [
        (event.type, event.payload) for event in events]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "Approved edit."
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_sealed_edit_file_without_a_recorded_read_is_refused_safely(monkeypatch, tmp_path):
    """M5.2 read-before-edit: even an approved edit of an existing file is
    refused when the session never recorded a read — fail-closed, with the
    actionable reread guidance, and the approval is consumed by the attempt."""
    import src.agent_loop as loop
    content = json.dumps({
        "path": "a.txt",
        "old_string": "Original text.",
        "new_string": "Approved edit.",
    })
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path, tool="edit_file", content=content,
    )
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    import src.tool_execution as execution
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: owner == "alice")

    async def provider(*args, **kwargs):
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "exact-edit-file",
                "name": pending.tool_name,
                "arguments": pending.content,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    # The file is untouched: no read baseline, no edit.
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "Original text."
    assert any("No recorded read" in json.dumps(event.payload) for event in events), [
        (event.type, event.payload) for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "file_replaced", "alias_retargeted", "parent_replaced",
    "root_replaced", "different_workspace", "disabled_tool",
    "account_denied", "adapter_surface_removed", "missing_binding",
    "missing_origin", "foreign_root", "changed_request_id",
])
async def test_changed_or_unsealed_filesystem_action_keeps_approval(
    monkeypatch, tmp_path, change,
):
    if change == "alias_retargeted":
        target = "alias"
    elif change == "parent_replaced":
        target = "folder/a.txt"
    else:
        target = "a.txt"

    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path, content=target,
    )
    if change == "alias_retargeted":
        (workspace / "alias").symlink_to(workspace / "a.txt")
    elif change == "parent_replaced":
        (workspace / "folder").mkdir()
        (workspace / "folder" / "a.txt").write_text("Original text.")
    if change in {"alias_retargeted", "parent_replaced"}:
        # Regenerate the approval once while the fixture target is valid.
        # This is still proposal-time sealing, never a resumed reconstruction.
        store.retire_for_session(owner="alice", session_id=session.id)
        pending = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="fs-fixture-run-2", tool_name="read_file",
            content=target, workspace=str(workspace),
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("read_file", target),
            selected_tools={"read_file"}, continuation_query="Read the sealed path.",
            request_text="Read the sealed path.",
            request_authority=RequestAuthority.empty(
                owner="alice", session_id=session.id, workspace=str(workspace),
            ),
        )
        options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")

    assert pending.resource_operation is not None, change

    if change == "file_replaced":
        (workspace / "a.txt").rename(workspace / "old-a.txt")
        (workspace / "a.txt").write_text("Replaced")
    elif change == "alias_retargeted":
        (workspace / "alias").unlink()
        (workspace / "alias").symlink_to(workspace / "b.txt")
    elif change == "parent_replaced":
        (workspace / "folder").rename(workspace / "old-folder")
        (workspace / "folder").mkdir()
        (workspace / "folder" / "a.txt").write_text("Replacement")
    elif change == "root_replaced":
        workspace.rename(tmp_path / "old-workspace")
        workspace.mkdir()
        (workspace / "a.txt").write_text("Impostor")
    elif change == "different_workspace":
        other = tmp_path / "another"
        other.mkdir()
        session.cwd = str(other)
    elif change == "disabled_tool":
        options["settings"] = replace(
            options["settings"], global_disabled_tools=("read_file",),
        )
    elif change == "account_denied":
        options["settings"] = replace(
            options["settings"], privileges={"can_use_agent": False},
        )
    elif change == "adapter_surface_removed":
        options["settings"] = replace(
            options["settings"], offered_tools=frozenset(),
        )
    elif change == "missing_binding":
        object.__setattr__(pending, "resource_operation", None)
    elif change == "missing_origin":
        object.__setattr__(pending, "request_authority", None)
    elif change == "foreign_root":
        from src.agent_runtime.resource_binding import resolve_filesystem_operation
        from src.agent_runtime.resources import FilesystemRoot
        from src.agent_runtime.authority import ExactOperation
        other = tmp_path / "outside"
        other.mkdir()
        (other / "a.txt").write_text("Other")
        foreign = resolve_filesystem_operation(
            ExactOperation.normalize("read_file", "a.txt"),
            roots=(FilesystemRoot.seal(other, owner="alice"),),
            workspace=str(other),
            request_id=pending.request_authority.request_id,
        )
        object.__setattr__(pending, "resource_operation", foreign)
    elif change == "changed_request_id":
        object.__setattr__(
            pending.resource_operation, "request_id", "different-request",
        )

    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations and not session.messages


@pytest.mark.asyncio
async def test_filesystem_changed_during_async_context_is_not_consumed(
    monkeypatch, tmp_path,
):
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
    )
    original = ports.preprocess_message

    async def replace_resource(*args, **kwargs):
        result = await original(*args, **kwargs)
        (workspace / "a.txt").rename(workspace / "old-a.txt")
        (workspace / "a.txt").write_text("Replacement", encoding="utf-8")
        return result

    monkeypatch.setattr(ports, "preprocess_message", replace_resource)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "Replacement"


@pytest.mark.asyncio
async def test_ordinary_turn_does_not_inherit_pending_filesystem_grant(
    monkeypatch, tmp_path,
):
    import src.turn_service as runtime
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
    )
    options["request"] = TextTurnRequest("List my notes without touching files")
    options["settings"] = replace(
        options["settings"], offered_tools=frozenset({"manage_notes"}),
    )
    seen = []

    async def fake_loop(*args, **kw):
        seen.append(kw)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", fake_loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert len(seen) == 1
    assert seen[0]["tool_policy"].blocks("read_file")
    assert seen[0]["tool_policy"].blocks("write_file")
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_explicit_deny_with_missing_filesystem_context_still_retires(
    monkeypatch, tmp_path,
):
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
    )
    session.cwd = None
    options["settings"] = replace(options["settings"], workspace=None)
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "deny")
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert [event.type for event in events] == [
        "approval.decided", "message.final", "turn.completed",
    ]
    assert store.peek(pending.approval_id) is None
    assert not ports.operations


@pytest.mark.asyncio
async def test_cancel_before_consumption_preserves_sealed_filesystem_card(
    monkeypatch, tmp_path,
):
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
    )
    entered = asyncio.Event()

    async def hold(*a, **k):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ports, "preprocess_message", hold)
    task = asyncio.create_task(collect(admission, session, options))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.peek(pending.approval_id) is pending
    assert not session.messages


@pytest.mark.asyncio
async def test_public_filesystem_policy_still_blocks_an_approved_write(
    monkeypatch, tmp_path,
):
    import src.agent_loop as loop
    import src.tool_execution as execution

    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path,
        tool="write_file", content="protected.txt\nMust not be written.",
    )
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: False)

    async def model(*args, **kwargs):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", model)
    delivery, events = await collect(admission, session, options)
    assert any(event.type == "tool.failed" for event in events)
    assert not any(event.type == "tool.completed" for event in events)
    assert not (workspace / "protected.txt").exists()
    assert store.peek(pending.approval_id) is None
    assert delivery.outcome.state == "completed"
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,content", [
    ("ls", "a.txt"),
    ("grep", json.dumps({"path": ".", "pattern": "Original"})),
    ("glob", json.dumps({"path": ".", "pattern": "*.txt"})),
    ("edit_file", json.dumps({"path": "a.txt", "old_text": "Original", "new_text": "Replaced"})),
    ("apply_patch", "*** Begin Patch\n*** Update File: a.txt\n@@\n-Original text.\n+Updated text.\n*** End Patch"),
])
async def test_other_filesystem_tools_retain_exact_presealed_context(
    monkeypatch, tmp_path, tool, content,
):
    import src.turn_service as runtime
    workspace, admission, session, ports, options, store, pending = sealed_turn(
        monkeypatch, tmp_path, tool=tool, content=content,
    )
    assert tool in NATIVE_FILESYSTEM_TOOLS
    assert pending.resource_operation is not None

    observed = []

    async def synthetic_loop(*args, **kwargs):
        assert kwargs["workspace"] == str(workspace)
        assert kwargs["turn_contract"].permits(tool)
        assert kwargs["turn_contract"].offered == frozenset({tool})
        assert kwargs["request_authority"].bound_to(
            owner="alice", session_id=session.id, workspace=str(workspace),
        )
        exact = kwargs["exact_approval"]
        assert exact.pending is pending
        assert exact.claim(
            owner="alice", session_id=session.id, workspace=str(workspace),
            tool_name=tool, content=content,
        )
        observed.append(True)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", synthetic_loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert observed == [True]
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_approval_does_not_restore_other_originally_selected_tool_families(
    monkeypatch, tmp_path,
):
    import src.turn_service as runtime

    workspace, admission, session, ports, options, store, retired = sealed_turn(
        monkeypatch, tmp_path,
    )
    pending = store.create(
        owner="alice", session_id=session.id,
        origin_run_id="multiple-tools-at-proposal",
        tool_name="read_file", content="a.txt",
        workspace=str(workspace),
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("read_file", "a.txt"),
        selected_tools={"read_file", "bash"},
        continuation_query="Read the approved file.",
        request_text="Read the approved file.",
        request_authority=RequestAuthority.empty(
            owner="alice", session_id=session.id, workspace=str(workspace),
        ),
    )
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")
    options["settings"] = replace(
        options["settings"],
        offered_tools=frozenset({"read_file", "bash"}),
    )
    seen = []

    async def synthetic_loop(*args, **kwargs):
        assert kwargs["turn_contract"].offered == frozenset({"read_file"})
        assert not kwargs["turn_contract"].permits("bash")
        assert kwargs["relevant_tools"] == {"read_file"}
        seen.append(True)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", synthetic_loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert seen == [True]
    assert store.peek(pending.approval_id) is None
