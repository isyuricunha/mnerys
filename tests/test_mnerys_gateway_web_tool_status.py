"""In-process, non-actionable native tool timeline parity with the Web UI.

No real model, remote client, browser approval card, credential or production
writer is used. Tool execution is still owned by the original Agent Loop.
"""

from dataclasses import replace
import json

import pytest

from src import turn_service as runtime
from src.agent_core_events import CoreApprovalEvent
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings
from src.gateway_turn_events import TurnEvent
from src.gateway_turns import GatewayTurns
from src.gateway_web_compat_events import serialize_plain_text_web_sse
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_composed_approvals import setup


async def generator(*events):
    for item in events:
        yield item


def decode(frame):
    return json.loads(next(
        part[6:] for part in frame.splitlines() if part.startswith("data: ")
    ))


@pytest.mark.asyncio
async def test_real_composer_native_tool_timeline_exposes_no_args_or_output(
    monkeypatch,
):
    admission, session, ports, options = configured(monkeypatch)
    model_calls = []

    async def loop(url, model, messages, **kw):
        model_calls.append(kw)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes",
            "round": 1, "command": "PRIVATE_COMMAND",
        })
        yield tool_event_frame({
            "type": "tool_progress", "tool": "manage_notes",
            "round": 1, "message": "PRIVATE_PROGRESS", "tail": "PRIVATE_TAIL",
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "output": "PRIVATE_RESULT", "exit_code": 0,
        })
        yield 'data: {"delta":"Three notes were found."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", loop)
    rendered = [
        frame async for frame in GatewayTurns(TurnService())
        .preview_composed_tool_status_web_sse(admission, session, **options)
    ]
    assert [decode(frame) if "[DONE]" not in frame else "[DONE]"
            for frame in rendered] == [
        {"type": "tool_start", "tool": "manage_notes", "command": ""},
        {
            "type": "tool_progress", "tool": "manage_notes",
            "message": "Tool is running",
        },
        {
            "type": "tool_output", "tool": "manage_notes",
            "command": "", "output": "", "exit_code": 0,
        },
        {"delta": "Three notes were found."},
        {"type": "final_response", "content": "Three notes were found."},
        "[DONE]",
    ]
    assert len(model_calls) == 1
    assert model_calls[0]["tool_policy"].blocks("bash")
    assert model_calls[0]["request_authority"].bound_to(
        owner="alice", session_id=session.id,
    )
    assert "PRIVATE_" not in repr(rendered)
    assert not session.messages
    assert not ports.private


@pytest.mark.asyncio
@pytest.mark.parametrize("end,code,expected", [
    ("tool.completed", None, 0),
    ("tool.failed", None, 1),
    ("tool.failed", 3, 3),
    ("tool.blocked", None, 1),
    ("tool.blocked", 0, 1),
])
async def test_browser_status_never_fabricates_tool_result(end, code, expected):
    events = [
        TurnEvent(1, "tool.started", {
            "tool": "manage_notes", "round": 3, "exit_code": None,
        }),
        TurnEvent(2, end, {
            "tool": "manage_notes", "round": 3, "exit_code": code,
        }),
        TurnEvent(3, "message.final", {"text": "The action finished."}),
        TurnEvent(4, "turn.completed", {}),
    ]
    chunks = [
        chunk async for chunk in serialize_plain_text_web_sse(
            generator(*events),
            require_saved_receipt=False,
            tool_status_preview=frozenset({"manage_notes"}),
        )
    ]
    assert decode(chunks[0])["command"] == ""
    assert decode(chunks[1]) == {
        "type": "tool_output", "tool": "manage_notes",
        "command": "", "output": "", "exit_code": expected,
    }
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("events,why", [
    (
        [TurnEvent(1, "tool.completed", {
            "tool": "manage_notes", "round": 1, "exit_code": 0,
        })],
        "started",
    ),
    (
        [TurnEvent(1, "tool.started", {
            "tool": "bash", "round": 1, "exit_code": None,
        })],
        "not admitted",
    ),
    (
        [TurnEvent(1, "tool.started", {
            "tool": "manage_notes", "round": 1, "exit_code": 0,
        })],
        "Overlapping or invalid",
    ),
    (
        [
            TurnEvent(1, "tool.started", {
                "tool": "manage_notes", "round": 1, "exit_code": None,
            }),
            TurnEvent(2, "tool.started", {
                "tool": "manage_notes", "round": 1, "exit_code": None,
            }),
        ],
        "Overlapping",
    ),
    (
        [
            TurnEvent(1, "tool.started", {
                "tool": "manage_notes", "round": 1, "exit_code": None,
            }),
            TurnEvent(2, "tool.completed", {
                "tool": "manage_notes", "round": 2, "exit_code": 0,
            }),
        ],
        "does not match",
    ),
    (
        [
            TurnEvent(1, "tool.started", {
                "tool": "manage_notes", "round": 1, "exit_code": None,
            }),
            TurnEvent(2, "turn.completed", {}),
        ],
        "without a terminal tool status",
    ),
    (
        [TurnEvent(1, "tool.awaiting_approval", {
            "tool": "manage_notes", "round": 1, "exit_code": None,
        })],
        "Unsupported tool status",
    ),
    (
        [TurnEvent(1, "approval.requested", {
            "approval_id": "x" * 43,
        })],
        "Pending or unresolved",
    ),
    (
        [TurnEvent(1, "turn.awaiting_approval", {
            "approval_id": "x" * 43,
        })],
        "no approved plain-text",
    ),
    (
        [TurnEvent(1, "tool.started", {
            "tool": "manage_notes", "round": 1,
            "exit_code": None, "ui_event": {"action": "delete"},
        })],
        "Invalid native tool status",
    ),
])
async def test_unsupported_tool_or_approval_status_fails_closed(events, why):
    with pytest.raises(ValueError, match=why):
        _ = [f async for f in serialize_plain_text_web_sse(
            generator(*events), require_saved_receipt=False,
            tool_status_preview=frozenset({"manage_notes"}),
        )]


@pytest.mark.asyncio
async def test_consumed_sealed_denial_only_resolves_existing_card_not_creates_one(
    monkeypatch,
):
    admission, session, ports, options, store, pending = setup(
        monkeypatch, choice="deny",
    )
    options["settings"] = TrustedTextTurnSettings(
        offered_tools=frozenset({"manage_notes"}),
    )
    rendered = [
        frame async for frame in GatewayTurns(TurnService())
        .preview_composed_tool_status_web_sse(
            admission, session, **options,
        )
    ]
    assert [decode(frame) if "[DONE]" not in frame else "[DONE]"
            for frame in rendered] == [
        {"type": "tool_approval_resolved", "decision": "deny"},
        {"type": "final_response", "content": "The action was denied."},
        "[DONE]",
    ]
    assert store.peek(pending.approval_id) is None
    assert len(session.messages) == 0
    assert not ports.operations
    assert not any("approval_id" in item for item in rendered)
    with pytest.raises(AdmissionDenied):
        _ = [
            frame async for frame in GatewayTurns(TurnService())
            .preview_composed_tool_status_web_sse(
                admission, session, **options,
            )
        ]


@pytest.mark.asyncio
async def test_pending_approval_is_not_deserialized_into_an_actionable_card(
    monkeypatch,
):
    admission, session, ports, options, store, pending = setup(
        monkeypatch, choice="deny",
    )
    options["request"] = TextTurnRequest("List my notes")
    options["settings"] = TrustedTextTurnSettings(
        offered_tools=frozenset({"manage_notes"}),
    )

    created = []

    async def loop(*args, **kwargs):
        # The composer intentionally retires stale cards on a new turn.
        # Mint a fresh *sealed* pending record at the original Loop boundary.
        from src.agent_runtime.authority import RequestAuthority
        current = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="new-native-run",
            tool_name="manage_notes", content='{"action":"list"}',
            workspace=None, request_text="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action(
                "manage_notes", '{"action":"list"}',
            ),
            request_authority=RequestAuthority.empty(
                owner="alice", session_id=session.id,
            ),
        )
        created.append(current)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes",
        })
        yield approval_requested_event(current)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", loop)
    stream = GatewayTurns(TurnService()).preview_composed_tool_status_web_sse(
        admission, session, **options,
    )
    with pytest.raises(ValueError, match="Pending or unresolved"):
        _ = [frame async for frame in stream]
    assert len(created) == 1
    assert store.peek(created[0].approval_id) is created[0]
    assert store.peek(pending.approval_id) is None
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "no_tools", "unexpected_tool", "workspace", "document", "attachments",
    "foreign_owner", "invalid_request", "untrusted_settings",
])
async def test_tool_preview_rejects_surface_escalation_before_context_io(
    monkeypatch, invalid,
):
    admission, session, ports, options = configured(monkeypatch)
    if invalid == "no_tools":
        options["settings"] = TrustedTextTurnSettings()
    elif invalid == "unexpected_tool":
        options["settings"] = TrustedTextTurnSettings(
            offered_tools=frozenset({"bash"}),
        )
    elif invalid == "workspace":
        options["settings"] = replace(
            options["settings"], workspace="/tmp",
        )
    elif invalid == "document":
        options["settings"] = replace(
            options["settings"], active_document_id="doc",
        )
    elif invalid == "attachments":
        options["request"] = TextTurnRequest(
            "List notes", ("note.txt",),
        )
    elif invalid == "foreign_owner":
        options["context_owner"] = "mallory"
    elif invalid == "invalid_request":
        options["request"] = TextTurnRequest("")
    else:
        options["settings"] = {"offered_tools": {"manage_notes"}}
    with pytest.raises(AdmissionDenied):
        GatewayTurns(TurnService()).preview_composed_tool_status_web_sse(
            admission, session, **options,
        )
    assert ports.operations == []
    assert not session.messages


@pytest.mark.asyncio
async def test_browser_event_representation_does_not_forward_sse_injection():
    raw = '<<<\n\nid: forged\nevent: error\ndata: privilege\n>>>'
    # The safe tool status type cannot carry raw command/output at all.
    with pytest.raises(ValueError, match="Invalid native tool status"):
        _ = [f async for f in serialize_plain_text_web_sse(
            generator(TurnEvent(1, "tool.started", {
                "tool": "manage_notes", "round": 0,
                "exit_code": None, "command": raw,
            })),
            tool_status_preview=frozenset({"manage_notes"}),
            require_saved_receipt=False,
        )]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"approval_id": "x" * 43, "decision": ["approve"]},
    {"approval_id": "x" * 43, "decision": "unseal"},
    {"approval_id": "../unsafe", "decision": "deny"},
    {"approval_id": "x" * 43, "decision": "deny", "tool": "bash"},
])
async def test_malformed_consumed_decisions_never_change_web_controls(payload):
    with pytest.raises(ValueError, match="Invalid consumed Core approval decision"):
        _ = [f async for f in serialize_plain_text_web_sse(
            generator(TurnEvent(1, "approval.decided", payload)),
            require_saved_receipt=False,
            tool_status_preview=frozenset({"manage_notes"}),
        )]
