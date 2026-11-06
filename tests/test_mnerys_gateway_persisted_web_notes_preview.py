"""Process-local persisted notes-to-Web SSE acceptance; no production route.

The ASGI endpoint below exists only inside the test app. It deliberately does
not claim authenticated HTTP cutover, detached resume, or remote approvals.
"""

from dataclasses import replace
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx
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


def decoded(frames):
    result = []
    for frame in frames:
        if frame.strip() == "data: [DONE]":
            result.append({"type": "DONE"})
        else:
            result.append(json.loads(next(
                line.removeprefix("data: ") for line in frame.splitlines()
                if line.startswith("data: ")
            )))
    return result


async def pending_fixture(monkeypatch):
    admission, session, ports, options, writes, partials = native_fixture(monkeypatch)
    store = ToolApprovalStore()
    options["approval_store"] = store
    gateway = GatewayTurns(TurnService())
    pending = []
    async def model(_url, _model, _messages, **kwargs):
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="fixture-persisted-web-notes",
            tool_name="manage_notes", content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(record)
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": None, "output": "PRIVATE_NOTES",
        }, result={"ask_user": record.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    return gateway, admission, session, ports, options, store, pending, writes, partials


@pytest.mark.asyncio
async def test_persisted_pending_card_is_complete_and_does_not_execute(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await pending_fixture(monkeypatch)
    )
    cards = decoded([frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )])
    assert [card.get("type") for card in cards] == ["ask_user", "DONE"], cards
    assert cards[0]["data"] == pending[0].public_payload()
    assert store.peek(pending[0].approval_id) is pending[0]
    assert ports.operations.count("save_user") == 1
    assert [msg.role for msg in session.history] == ["user"]
    assert not writes and not partials
    assert "PRIVATE_NOTES" not in repr(cards)


@pytest.mark.asyncio
async def test_http_asgi_preview_only_approves_once_and_confirms_save(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await pending_fixture(monkeypatch)
    )
    first = decoded([frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )])
    assert first[0]["type"] == "ask_user"
    actions = []

    async def approved(_url, _model, messages, **kwargs):
        exact = kwargs["exact_approval"]
        assert exact.pending is pending[0]
        assert sum(item.get("content") == "List my notes" for item in messages) == 1
        assert exact.claim(
            owner="alice", session_id=session.id,
            tool_name="manage_notes", content=pending[0].content, workspace=None,
        )
        actions.append("approved once")
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0, "output": "PRIVATE_NOTES_CONTENT",
        })
        yield 'data: {"delta":"Notes listed."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(runtime, "stream_agent_loop", approved)
    options["request"] = ToolApprovalTurnRequest(pending[0].approval_id, "approve_task")

    # No production router is mounted: this ASGI transport tests only the
    # HTTP-facing serialization and the original admitted composer/writer.
    app = FastAPI()
    @app.post("/synthetic-notes-review")
    async def test_only_route():
        return StreamingResponse(
            gateway.preview_persisted_native_notes_web_sse(
                admission, session, **options,
            ),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://example.com",
    ) as client:
        response = await client.post("/synthetic-notes-review")
    assert response.status_code == 200
    frames = [json.loads(x[6:]) if x != "data: [DONE]" else {"type": "DONE"}
              for x in response.text.split("\n\n") if x and x.startswith("data: ")]
    assert [frame.get("type", "delta" if "delta" in frame else "unknown") for frame in frames] == [
        "tool_approval_resolved", "tool_start", "tool_output",
        "delta", "message_saved", "final_response", "DONE",
    ]
    assert frames[-3]["id"] == session.history[-1].metadata["_db_id"]
    assert frames[-2]["content"] == "Notes listed."
    assert actions == ["approved once"]
    assert store.peek(pending[0].approval_id) is None
    assert ports.operations.count("save_user") == 1
    assert [msg.role for msg in session.history] == ["user", "assistant"]
    assert len(writes) == 1 and not partials
    assert "PRIVATE_NOTES_CONTENT" not in response.text
    with pytest.raises(AdmissionDenied):
        replay = gateway.preview_persisted_native_notes_web_sse(
            admission, session, **options,
        )
        _ = [frame async for frame in replay]
    assert actions == ["approved once"]


@pytest.mark.asyncio
async def test_web_review_deny_persists_once_without_model(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await pending_fixture(monkeypatch)
    )
    _ = [frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )]
    async def prohibited(*args, **kwargs):
        raise AssertionError("Denied request called model")
        yield
    monkeypatch.setattr(runtime, "stream_agent_loop", prohibited)
    options["request"] = ToolApprovalTurnRequest(pending[0].approval_id, "deny")
    frames = decoded([frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )])
    assert [frame.get("type", "delta" if "delta" in frame else "unknown") for frame in frames] == [
        "tool_approval_resolved", "message_saved", "final_response", "DONE",
    ]
    assert frames[-2]["content"] == "The action was denied."
    assert [msg.role for msg in session.history] == ["user", "assistant"]
    assert ports.operations.count("save_user") == 1
    assert store.peek(pending[0].approval_id) is None
    assert len(writes) == 1 and not partials


@pytest.mark.asyncio
async def test_web_notes_preview_writer_failure_has_no_false_save_ack(monkeypatch):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await pending_fixture(monkeypatch)
    )
    first = [frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )]
    assert any('"ask_user"' in frame for frame in first)
    effects, write_attempts = [], []

    async def approved(_url, _model, _messages, **kwargs):
        assert kwargs["exact_approval"].claim(
            owner="alice", session_id=session.id,
            tool_name="manage_notes", content=pending[0].content, workspace=None,
        )
        effects.append(True)
        yield 'data: {"delta":"Completed effect"}\n\n'
        yield "data: [DONE]\n\n"

    def fails_after_effect(*_args, **_kwargs):
        write_attempts.append(True)
        raise RuntimeError("private persistence failure")

    monkeypatch.setattr(runtime, "stream_agent_loop", approved)
    options["persistence"] = replace(options["persistence"], saver=fails_after_effect)
    options["request"] = ToolApprovalTurnRequest(pending[0].approval_id, "approve")
    frames = [frame async for frame in gateway.preview_persisted_native_notes_web_sse(
        admission, session, **options,
    )]
    assert any('"type":"tool_approval_resolved"' in frame for frame in frames)
    assert any('"delta":"Completed effect"' in frame for frame in frames)
    assert len([frame for frame in frames if "event: error" in frame]) == 1
    assert not any('"type":"message_saved"' in frame for frame in frames)
    assert not any('"type":"final_response"' in frame for frame in frames)
    assert not any("[DONE]" in frame for frame in frames)
    assert "private persistence failure" not in repr(frames)
    assert effects == write_attempts == [True]
    assert [msg.role for msg in session.history] == ["user"]
    assert not writes and not partials
    assert store.peek(pending[0].approval_id) is None
    with pytest.raises(AdmissionDenied):
        retry = gateway.preview_persisted_native_notes_web_sse(
            admission, session, **options,
        )
        _ = [frame async for frame in retry]
    assert effects == write_attempts == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "foreign", "no_persistence", "other_tool", "workspace",
    "fallback", "untrusted_flag", "attachments",
])
async def test_web_preview_rejects_unsupported_profile_without_effects(monkeypatch, invalid):
    gateway, admission, session, ports, options, store, pending, writes, partials = (
        await pending_fixture(monkeypatch)
    )
    if invalid == "foreign":
        session.owner = "other"
    elif invalid == "no_persistence":
        options["persistence"] = None
    elif invalid == "other_tool":
        options["settings"] = replace(
            options["settings"], offered_tools=frozenset({"bash"}),
        )
    elif invalid == "workspace":
        options["settings"] = replace(options["settings"], workspace="/tmp")
    elif invalid == "fallback":
        options["settings"] = replace(
            options["settings"], enable_model_fallback=True,
            allowed_fallback_models=frozenset({"other-model"}),
        )
    elif invalid == "untrusted_flag":
        options["settings"] = replace(
            options["settings"], persist_verified_native_tools="true",
        )
    else:
        from src.gateway_turn_composer import TextTurnRequest
        options["request"] = TextTurnRequest("List my notes", ("attachment",))
    with pytest.raises(AdmissionDenied):
        frames = gateway.preview_persisted_native_notes_web_sse(
            admission, session, **options,
        )
        _ = [frame async for frame in frames]
    assert not ports.operations
    assert not writes and not partials and not pending
