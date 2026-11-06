"""Server-selected context feature flags in the direct composer.

Covers memory/RAG/web/incognito/preprocessing toggles selected by a trusted
adapter. No HTTP app, database, external tool or live model is used.
"""

import pytest

from src import turn_service as turn_module
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import (
    ToolApprovalTurnRequest, TrustedTextTurnSettings,
)
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from test_mnerys_gateway_turn_composer import FakePorts, fixture, gather


class ProbingPorts(FakePorts):
    """Record the exact options the shared context algorithm received."""

    def __init__(self):
        super().__init__()
        self.private_transcript = []

    async def preprocess_message(
        self, _handler, message, attachment_ids, _session,
        *, auto_opened_docs, allow_tool_preprocessing,
    ):
        self.operations.append(("preprocess", allow_tool_preprocessing))
        from src.gateway_chat_context_types import PreprocessedMessage
        return PreprocessedMessage(message, message, message, [], [])

    def append_private_message(self, session_id, role, content, metadata):
        self.operations.append(("private", session_id, role, content))
        self.private_transcript.append({"role": role, "content": content})

    def private_messages(self, session_id):
        self.operations.append(("private_read", session_id))
        return list(self.private_transcript)


def feature_fixture(**flags):
    admission, session, ports, kwargs = fixture()
    ports = ProbingPorts()
    kwargs["ports"] = ports
    kwargs["settings"] = TrustedTextTurnSettings(**flags)
    return admission, session, ports, kwargs


async def answer_model(_endpoint, _model, _messages, **_options):
    yield 'data: {"delta":"Answer"}\n\n'
    yield "data: [DONE]\n\n"


def run(monkeypatch, admission, session, kwargs):
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    return GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    )


@pytest.mark.asyncio
async def test_default_flags_preserve_original_direct_profile(monkeypatch):
    admission, session, ports, kwargs = feature_fixture()
    chunks = await gather(run(monkeypatch, admission, session, kwargs))
    assert chunks[-1] == "data: [DONE]\n\n"
    assert ("preprocess", False) in ports.operations
    assert ports.preface_options["use_memory"] is False
    assert ports.preface_options["use_skills"] is False
    assert ports.preface_options["use_rag"] is False
    assert ports.preface_options["use_web"] is False
    assert ports.preface_options["incognito"] is False


@pytest.mark.asyncio
async def test_memory_and_rag_flags_reach_the_shared_context(monkeypatch):
    admission, session, ports, kwargs = feature_fixture(
        enable_memory=True, enable_rag=True, allow_tool_preprocessing=True,
    )
    await gather(run(monkeypatch, admission, session, kwargs))
    assert ("preprocess", True) in ports.operations
    assert ports.preface_options["use_memory"] is True
    assert ports.preface_options["use_rag"] is True
    assert "used_memories" in ports.operations


@pytest.mark.asyncio
async def test_web_flag_reaches_the_shared_context(monkeypatch):
    admission, session, ports, kwargs = feature_fixture(
        enable_web=True, allow_tool_preprocessing=True,
    )
    await gather(run(monkeypatch, admission, session, kwargs))
    assert ports.preface_options["use_web"] is True


@pytest.mark.asyncio
async def test_memory_without_preprocessing_stays_disabled(monkeypatch):
    # The inherited context algorithm suppresses memory/RAG when tool-driven
    # preprocessing is off; the flag alone cannot silently re-enable it.
    admission, session, ports, kwargs = feature_fixture(
        enable_memory=True, enable_rag=True,
    )
    await gather(run(monkeypatch, admission, session, kwargs))
    assert ports.preface_options["use_memory"] is False
    assert ports.preface_options["use_rag"] is False


@pytest.mark.asyncio
async def test_incognito_turn_uses_private_transcript_and_no_memory(monkeypatch):
    admission, session, ports, kwargs = feature_fixture(
        incognito=True, enable_memory=True, allow_tool_preprocessing=True,
    )
    ports.private_transcript.append({"role": "user", "content": "earlier"})
    await gather(run(monkeypatch, admission, session, kwargs))
    assert ("private_read", session.id) in ports.operations
    assert "history" not in ports.operations
    assert ports.preface_options["use_memory"] is False
    assert ports.preface_options["use_rag"] is False
    assert not session.messages


@pytest.mark.asyncio
async def test_incognito_turn_cannot_persist_conversation_messages(monkeypatch):
    admission, session, ports, kwargs = feature_fixture(incognito=True)
    from src.gateway_text_persistence import TrustedTextPersistence
    kwargs["persistence"] = TrustedTextPersistence(
        session_manager=object(),
        saver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no saves")),
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )
    with pytest.raises(AdmissionDenied, match="persist"):
        [event async for event in delivery.events]
    assert not session.messages


@pytest.mark.asyncio
async def test_incognito_turn_cannot_resume_an_approval(monkeypatch):
    admission, session, ports, kwargs = feature_fixture(incognito=True)
    kwargs["request"] = ToolApprovalTurnRequest("approval-1", "approve")
    with pytest.raises(AdmissionDenied, match="Incognito"):
        [chunk async for chunk in run(monkeypatch, admission, session, kwargs)]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [
    "enable_memory", "enable_rag", "enable_web", "incognito",
    "allow_tool_preprocessing",
])
async def test_non_boolean_feature_flags_are_rejected(monkeypatch, flag):
    admission, session, ports, kwargs = feature_fixture(**{flag: "true"})
    with pytest.raises(AdmissionDenied, match="feature flags"):
        [chunk async for chunk in run(monkeypatch, admission, session, kwargs)]
