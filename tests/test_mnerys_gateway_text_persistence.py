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
        yield 'event: error\ndata: {"status":502,"detail":"internal secret"}\n\n'
        yield "data: [DONE]\n\n"

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
    assert delivery.outcome.failure_code == "provider_error"



def with_partial_writer(session, kwargs):
    partials = []

    def writer(current, manager, content, metadata):
        assert current is session
        assert manager is kwargs["persistence"].session_manager
        partials.append((content, metadata.copy()))
        current.messages.append({
            "role": "assistant", "content": content,
            "metadata": metadata.copy(),
        })

    kwargs["persistence"] = replace(
        kwargs["persistence"], interrupted_writer=writer,
    )
    return partials


@pytest.mark.asyncio
async def test_completed_turn_releases_final_only_after_assistant_write(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    order = []
    original = kw["persistence"].saver

    def saver(*args, **kwargs):
        original(*args, **kwargs)
        order.append("persisted")

    kw["persistence"] = replace(kw["persistence"], saver=saver)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    order.extend(e.type for e in events if e.type in ("message.final", "turn.completed"))
    assert order == ["persisted", "message.final", "turn.completed"]
    assert saved[0][0] == "answer"
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_writer_failure_never_yields_uncommitted_final(monkeypatch):
    admission, session, _ports, kw, _saved = persisted_fixture()

    def fail(*_a, **_kw):
        raise RuntimeError("sensitive database diagnostic")

    kw["persistence"] = replace(kw["persistence"], saver=fail)
    partials = with_partial_writer(session, kw)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert [e.sequence for e in events] == [1, 2]
    assert events[-1].payload == {"code": "persistence_error", "status": None}
    assert delivery.outcome.last_sequence == 2
    assert delivery.outcome.state == "failed"
    assert partials == []  # a failed final save might have already committed


@pytest.mark.asyncio
async def test_client_close_saves_only_visible_partial_then_resumes_history(monkeypatch):
    admission, session, ports, kw, saved = persisted_fixture()
    partials = with_partial_writer(session, kw)
    seen = []

    async def model(_endpoint, _model, messages, **_options):
        seen.append(list(messages))
        yield 'data: {"delta":"partial answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    delivery = gateway.open_composed_text_events(admission, session, **kw)
    assert (await anext(delivery.events)).type == "message.delta"
    await delivery.events.aclose()
    await delivery.events.aclose()
    assert saved == []
    assert partials == [(
        "partial answer",
        {"stopped": True, "interruption_reason": "cancelled"},
    )]
    assert delivery.outcome.state == "cancelled"
    assert [m["role"] for m in session.messages] == ["user", "assistant"]

    # A separate, explicitly new text turn reads the partial from the same
    # existing history provider. It does not replay the previous turn.
    next_kw = dict(kw, request=TextTurnRequest("Continue the answer"))
    next_delivery = gateway.open_composed_text_events(
        admission, session, **next_kw,
    )
    assert (await collect(next_delivery))[-1].type == "turn.completed"
    assert [m["content"] for m in seen[1] if m["role"] != "system"][-3:] == [
        "Explain how a queue works", "partial answer", "Continue the answer",
    ]
    assert len(partials) == 1
    assert saved[-1][0] == "partial answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_error", "incomplete_stream"])
async def test_failed_stream_saves_partial_once_and_keeps_failure_state(monkeypatch, failure):
    admission, session, _ports, kw, saved = persisted_fixture()
    partials = with_partial_writer(session, kw)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"visible start"}\n\n'
        if failure == "provider_error":
            yield 'event: error\ndata: {"status":502,"detail":"private diagnostic"}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert delivery.outcome.state == "failed"
    assert delivery.outcome.failure_code == failure
    assert partials == [(
        "visible start",
        {"stopped": True, "interruption_reason": failure},
    )]
    assert saved == []
    assert "private diagnostic" not in repr(events)


@pytest.mark.asyncio
async def test_partial_writer_failure_is_terminal_and_not_retried(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    attempts = []

    def fail_partial(*_args):
        attempts.append(True)
        raise RuntimeError("private partial write diagnostic")

    kw["persistence"] = replace(
        kw["persistence"], interrupted_writer=fail_partial,
    )

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"visible start"}\n\n'
        yield 'event: error\ndata: {"status":500}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert events[-1].payload == {"code": "persistence_error", "status": None}
    assert delivery.outcome.failure_code == "persistence_error"
    assert attempts == [True]
    assert saved == []


@pytest.mark.asyncio
async def test_partial_writer_error_preserves_cancel_and_no_auto_retry(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    attempts = []

    def fail_partial(*_args):
        attempts.append(True)
        raise RuntimeError("write failed")

    kw["persistence"] = replace(
        kw["persistence"], interrupted_writer=fail_partial,
    )

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"part"}\n\n'
        raise asyncio.CancelledError()

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    assert (await anext(delivery.events)).type == "message.delta"
    with pytest.raises(asyncio.CancelledError):
        await anext(delivery.events)
    await delivery.events.aclose()
    assert attempts == [True]
    assert saved == []
    assert delivery.outcome.state == "cancelled"


@pytest.mark.asyncio
async def test_invalid_interrupted_writer_fails_before_context_io():
    admission, session, ports, kw, saved = persisted_fixture()
    kw["persistence"] = replace(
        kw["persistence"], interrupted_writer=object(),
    )
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    with pytest.raises(AdmissionDenied):
        await collect(delivery)
    assert ports.operations == []
    assert saved == []


@pytest.mark.asyncio
async def test_no_visible_deltas_means_no_partial_write(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    partials = with_partial_writer(session, kw)

    async def model(*_args, **_kwargs):
        yield 'event: error\ndata: {"status":500}\n\n'

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert events[-1].type == "turn.failed"
    assert partials == []
    assert saved == []



@pytest.mark.asyncio
async def test_interrupted_writer_receives_only_filtered_visible_deltas(monkeypatch):
    admission, session, _ports, kw, saved = persisted_fixture()
    partials = with_partial_writer(session, kw)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"never-save-this-reasoning","thinking":true}\n\n'
        yield 'data: {"delta":"Visible answer"}\n\n'
        yield 'event: error\ndata: {"status":500,"detail":"hidden diagnostic"}\n\n'

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    events = await collect(delivery)
    assert [e.type for e in events] == ["message.delta", "turn.failed"]
    assert partials[0][0] == "Visible answer"
    assert "never-save" not in repr(partials)
    assert "hidden diagnostic" not in repr(partials)
    assert saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "error"])
async def test_owner_change_midstream_refuses_late_assistant_write(monkeypatch, ending):
    admission, session, ports, kw, saved = persisted_fixture()
    partials = with_partial_writer(session, kw)

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"candidate"}\n\n'
        if ending == "error":
            yield 'event: error\ndata: {"status":500}\n\n'
        else:
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kw,
    )
    assert (await anext(delivery.events)).type == "message.delta"
    session.owner = "different-owner"
    remaining = [event async for event in delivery.events]
    assert len(remaining) == 1
    assert remaining[0].type == "turn.failed"
    assert remaining[0].payload["code"] == "persistence_error"
    assert delivery.outcome.state == "failed"
    assert len(session.messages) == 1
    assert saved == []
    assert partials == []
    assert ports.operations.count("save_user") == 1
