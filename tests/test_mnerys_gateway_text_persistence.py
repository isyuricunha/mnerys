"""Opt-in direct text persistence with inherited writers, no live database."""

import asyncio
from dataclasses import replace

import pytest

from src import turn_service as turn_module
from src.gateway_admission import AdmissionDenied
from src.gateway_text_persistence import TrustedTextPersistence
from src.gateway_turn_composer import (
    TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings,
)
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from test_mnerys_gateway_turn_composer import FakePorts, fixture


class PersistPorts(FakePorts):
    def append_user_message(
        self, session, _handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        assert incognito is False
        assert interaction_mode == "agent"
        assert auto_escalated is False
        self.operations.append("save_user")
        session.messages.append({
            "role": "user", "content": preprocessed.user_content,
        })


def persisted_fixture():
    admission, session, _ports, kwargs = fixture()
    ports = PersistPorts()
    kwargs["ports"] = ports
    saved = []

    def saver(current, manager, session_id, response, metrics, **metadata):
        assert current is session
        assert manager is session_manager
        assert session_id == session.id
        saved.append((response, metrics, metadata))
        current.messages.append({"role": "assistant", "content": response})

    session_manager = object()
    kwargs["persistence"] = TrustedTextPersistence(session_manager, saver)
    return admission, session, ports, kwargs, saved


async def collect(delivery):
    return [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_completed_turn_persists_both_messages_once_after_final(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()
    prompt = []

    async def model(_endpoint, _model, messages, **_kwargs):
        prompt.append([dict(item) for item in messages])
        yield 'data: {"delta":"Hello"}\n\n'
        yield "data: [DONE]\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert delivery.outcome.state == "completed"
    assert [event.type for event in events].count("turn.completed") == 1
    assert saved and saved[0][0] == "Hello"
    assert session.messages == [
        {"role": "user", "content": kw["request"].message},
        {"role": "assistant", "content": "Hello"},
    ]
    assert sum(
        item["content"] == kw["request"].message
        for item in prompt[0] if item["role"] == "user"
    ) == 1
    assert ports.operations.count("save_user") == 1


@pytest.mark.asyncio
async def test_persisted_history_is_used_on_next_turn(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    seen = []

    async def model(_endpoint, _model, messages, **_options):
        seen.append(list(messages))
        yield 'data: {"type":"final_response","content":"Answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    for message in ["First question", "Second question"]:
        await collect(gateway.open_composed_text_events(
            admission, session, **dict(kw, request=TextTurnRequest(message)),
        ))
    assert len(saved) == 2
    assert [entry["content"] for entry in session.messages] == [
        "First question", "Answer", "Second question", "Answer",
    ]
    assert [entry["content"] for entry in seen[1]][-3:] == [
        "First question", "Answer", "Second question",
    ]


@pytest.mark.asyncio
async def test_failing_saver_returns_sanitized_failure_not_completion(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()

    def broken(*args, **kwargs):
        raise RuntimeError("writer diagnostic must not be exposed")

    kw["persistence"] = replace(kw["persistence"], saver=broken)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"Answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert events[-1].type == "turn.failed"
    assert events[-1].payload == {"code": "persistence_error", "status": None}
    assert not any(event.type == "turn.completed" for event in events)
    assert "SECRET" not in repr(events)
    assert delivery.outcome.state == "failed"
    assert delivery.outcome.failure_code == "persistence_error"
    assert ports.operations.count("save_user") == 1
    assert len(session.messages) == 1
    assert saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["close", "cancel", "error"])
async def test_unfinished_stream_never_saves_assistant(monkeypatch, ending):
    admission, session, ports, kw, saved = persisted_fixture()
    closed = []

    async def model(*_args, **_kwargs):
        try:
            yield 'data: {"delta":"partial"}\n\n'
            if ending == "cancel":
                raise asyncio.CancelledError()
            if ending == "error":
                raise RuntimeError("failed")
            yield "data: [DONE]\n\n"
        finally:
            closed.append(True)

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    assert (await anext(delivery.events)).type == "message.delta"
    if ending == "close":
        await delivery.events.aclose()
    else:
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else RuntimeError):
            await anext(delivery.events)
        await delivery.events.aclose()
    assert closed == [True]
    assert saved == []
    assert len(session.messages) == 1
    assert ports.operations.count("save_user") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["approval", "tools", "writer", "foreign"])
async def test_persisted_mode_refuses_unsupported_requests_before_context_io(invalid):
    admission, session, ports, kw, saved = persisted_fixture()
    if invalid == "approval":
        kw["request"] = ToolApprovalTurnRequest("any-approval", "approve")
    elif invalid == "tools":
        kw["settings"] = TrustedTextTurnSettings(offered_tools=frozenset({"notes"}))
    elif invalid == "writer":
        kw["persistence"] = TrustedTextPersistence(None, lambda *args: None)
    else:
        kw["context_owner"] = "mallory"
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    with pytest.raises(AdmissionDenied):
        await collect(delivery)
    assert ports.operations == []
    assert session.messages == []
    assert saved == []


@pytest.mark.asyncio
async def test_unsupported_preprocessed_content_fails_without_writing(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()

    async def multimodal(_handler, message, attachments, _session, *,
                         auto_opened_docs, allow_tool_preprocessing):
        from src.gateway_chat_context_types import PreprocessedMessage
        return PreprocessedMessage(message, [{"type": "text", "text": message}],
                                   message, [], [])

    monkeypatch.setattr(ports, "preprocess_message", multimodal)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    with pytest.raises(ValueError, match="text-only"):
        await collect(delivery)
    assert "save_user" not in ports.operations
    assert session.messages == []
    assert saved == []


@pytest.mark.asyncio
async def test_provider_failure_does_not_persist_assistant(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()

    async def failed(*_args, **_kwargs):
        yield 'event: error\\ndata: {"status":502,"detail":"internal secret"}\\n\\n'
        yield "data: [DONE]\\n\\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", failed)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert delivery.outcome.state == "failed"
    assert not any(event.type == "turn.completed" for event in events)
    assert saved == []
    assert len(session.messages) == 1
    assert "save_user" in ports.operations
    assert "internal secret" not in repr(events)
