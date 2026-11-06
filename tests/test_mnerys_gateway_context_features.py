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


def email_fixture(**flags):
    admission, session, ports, kwargs = feature_fixture(**flags)
    return admission, session, ports, kwargs


options_received: dict = {}


async def options_model(_endpoint, _model, _messages, **options):
    options_received.update(options)
    yield 'data: {"delta":"Answer"}\n\n'
    yield "data: [DONE]\n\n"


ACTIVE_EMAIL = {"uid": "42", "folder": "INBOX", "account": "work"}


@pytest.mark.asyncio
async def test_active_email_context_reaches_the_agent_loop(monkeypatch):
    admission, session, _ports, kwargs = email_fixture(
        interaction_mode="agent",
        offered_tools=frozenset({"manage_notes"}),
        active_email=ACTIVE_EMAIL,
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["active_email"] == ACTIVE_EMAIL


@pytest.mark.asyncio
async def test_active_email_review_denies_compose_tools(monkeypatch):
    admission, session, _ports, kwargs = email_fixture(
        interaction_mode="agent",
        offered_tools=frozenset({
            "manage_notes", "send_email", "reply_to_email", "create_document",
        }),
        active_email=ACTIVE_EMAIL,
    )
    received = {}

    async def model(_endpoint, _model, _messages, **options):
        received.update(options)
        yield 'data: {"delta":"Answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    policy = received["tool_policy"]
    assert policy.blocks("send_email")
    assert policy.blocks("reply_to_email")
    assert policy.blocks("create_document")
    assert not policy.blocks("manage_notes")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_context", [
    {"folder": "INBOX"},                                  # missing uid
    {"uid": "  "},                                        # blank uid
    {"uid": "42", "extra": "x"},                          # unknown key
    {"uid": "42", "subject": 7},                          # non-string field
    {"uid": "x" * 300},                                   # oversized uid
    {"uid": "42", "body_preview": "y" * 3000},            # oversized preview
])
async def test_invalid_active_email_context_is_rejected(monkeypatch, bad_context):
    admission, session, _ports, kwargs = email_fixture(active_email=bad_context)
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    with pytest.raises(AdmissionDenied, match="email"):
        [chunk async for chunk in GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kwargs,
        )]


@pytest.mark.asyncio
async def test_without_active_email_the_loop_receives_none(monkeypatch):
    admission, session, _ports, kwargs = email_fixture()
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["active_email"] is None


class PresetPorts(ProbingPorts):
    def __init__(self):
        super().__init__()
        self.preset_ids = []

    def extract_preset(self, _handler, preset_id):
        self.preset_ids.append(preset_id)
        from src.gateway_chat_context_types import PresetInfo
        return PresetInfo(None, None, None, None)


@pytest.mark.asyncio
async def test_preset_id_reaches_the_shared_context(monkeypatch):
    admission, session, _ports, kwargs = feature_fixture(preset_id="coder")
    ports = PresetPorts()
    kwargs["ports"] = ports
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert ports.preset_ids == ["coder"]


@pytest.mark.asyncio
async def test_request_options_reach_the_agent_loop(monkeypatch):
    admission, session, _ports, kwargs = feature_fixture(
        thinking_mode="on",
        reasoning_effort="high",
        temperature=0.3,
        max_tool_calls=7,
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["thinking_mode"] == "on"
    assert options_received["reasoning_effort"] == "high"
    assert options_received["temperature"] == 0.3
    assert options_received["max_tool_calls"] == 7


@pytest.mark.asyncio
async def test_composed_temperature_defaults_match_the_web_turn(monkeypatch):
    admission, session, _ports, kwargs = feature_fixture()
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["temperature"] == 1.0
    assert options_received["thinking_mode"] is None
    assert options_received["approved_plan"] is None


@pytest.mark.asyncio
async def test_plan_mode_reaches_policy_and_loop(monkeypatch):
    admission, session, _ports, kwargs = feature_fixture(
        interaction_mode="agent",
        offered_tools=frozenset({"manage_notes"}),
        plan_mode=True,
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["plan_mode"] is True


@pytest.mark.asyncio
async def test_approved_plan_is_pinned_into_the_loop(monkeypatch):
    admission, session, _ports, kwargs = feature_fixture(
        interaction_mode="agent",
        offered_tools=frozenset({"manage_notes"}),
        approved_plan="1. do this",
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kwargs,
    ))
    assert options_received["approved_plan"] == "1. do this"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_settings,match", [
    ({"thinking_mode": "maybe"}, "request options"),
    ({"reasoning_effort": "High"}, "request options"),
    ({"temperature": 3.0}, "request options"),
    ({"max_tool_calls": 0}, "request options"),
    ({"preset_id": ""}, "request options"),
    ({"plan_mode": True, "interaction_mode": "chat"}, "mode modifiers"),
    ({"plan_mode": True, "interaction_mode": "agent", "approved_plan": "x"}, "mode modifiers"),
    ({"approved_plan": "x" * 9000}, "mode modifiers"),
    ({"compare_mode": True, "interaction_mode": "agent"}, "mode modifiers"),
    ({"search_context": "x" * 70000}, "mode modifiers"),
])
async def test_invalid_request_options_are_rejected(monkeypatch, bad_settings, match):
    admission, session, _ports, kwargs = feature_fixture(**bad_settings)
    monkeypatch.setattr(turn_module, "stream_agent_loop", options_model)
    with pytest.raises(AdmissionDenied, match=match):
        [chunk async for chunk in GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kwargs,
        )]
