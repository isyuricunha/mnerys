"""Direct context + policy + authority + Agent Loop acceptance without Web.

No HTTP app, database connection, external tool or live model is used.
Tests prove the first non-persisting, no-tools direct application seam only.
"""

import ast
import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src import turn_service as turn_module
from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_chat_context_types import PresetInfo, PreprocessedMessage
from src.gateway_turn_composer import (
    TextTurnRequest, TrustedTextTurnSettings,
)
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService


class FakePorts:
    """Complete isolated implementation of the shared context I/O ports."""

    def __init__(self):
        self.operations = []
        self.private = []
        self.preface_options = {}

    def extract_preset(self, _handler, _preset):
        self.operations.append("preset")
        return PresetInfo(None, None, None, None)

    async def preprocess_message(
        self, _handler, message, attachment_ids, _session,
        *, auto_opened_docs, allow_tool_preprocessing,
    ):
        assert attachment_ids == []
        assert allow_tool_preprocessing is False
        self.operations.append("preprocess")
        return PreprocessedMessage(message, message, message, [], [])

    def append_user_message(self, *_a, **_kw):
        raise AssertionError("Direct pre-cutover turn persisted a user message")

    def append_private_message(self, *_a, **_kw):
        raise AssertionError("Direct pre-cutover turn wrote incognito data")

    def private_messages(self, *_a):
        return []

    def load_preferences(self, owner):
        self.operations.append(("prefs", owner))
        return {"memory_enabled": True, "skills_enabled": True}

    def upload_manifest(self, ids, _handler, owner):
        assert not ids
        self.operations.append(("manifest", owner))
        return []

    def build_preface(self, _processor, **options):
        self.operations.append("preface")
        self.preface_options = options
        return ([{"role": "system", "content": "trusted fixture preface"}], [], [])

    def used_memories(self, _processor):
        self.operations.append("used_memories")
        return []

    async def compact(
        self, _session, _url, _model, messages, _headers, *, owner, **_options,
    ):
        self.operations.append(("compact", owner))
        return messages, 16384, False

    def trim(self, messages, _length):
        return messages

    def youtube_sources(self, _message, _transcripts):
        return []

    def normalize_model(self, _session):
        return None

    def session_history(self, session):
        self.operations.append("history")
        return list(session.messages)

    def current_datetime_message(self):
        self.operations.append("clock")
        return {"role": "user", "content": "fixture clock"}

    def context_length(self, _url, _model):
        return 32768

    def estimate_tokens(self, messages):
        return len(messages) * 10


def fixture(credential="browser"):
    principal = Principal(
        None if credential == "single_user" else "alice",
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    admission = GatewayAdmission().admit(
        principal, "session-1",
        lambda _id: SessionOwnership(owner_id="alice"),
    )
    session = SimpleNamespace(
        id="session-1", owner=principal.owner_id, name="Direct",
        endpoint_url="https://example.com/v1", model="fixture-model",
        headers={"Authorization": "redacted"},
        history=[], messages=[],
    )
    ports = FakePorts()
    kwargs = dict(
        context_owner=principal.owner_id,
        request=TextTurnRequest("Explain how a queue works"),
        ports=ports,
        chat_handler=SimpleNamespace(upload_handler=None),
        chat_processor=SimpleNamespace(),
        settings=TrustedTextTurnSettings(),
    )
    return admission, session, ports, kwargs


async def gather(stream):
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
async def test_one_gateway_call_composes_context_policy_authority_and_executes(
    monkeypatch, credential,
):
    admission, session, ports, kwargs = fixture(credential)
    received = {}

    async def model(*args, **options):
        received["args"] = args
        received["options"] = options
        yield 'data: {"delta":"Fixture answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    chunks = await gather(
        GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kwargs,
        )
    )
    assert chunks == [
        'data: {"delta":"Fixture answer"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert received["args"][0:2] == (session.endpoint_url, session.model)
    assert received["args"][2][-1]["content"] == kwargs["request"].message
    assert received["options"]["headers"] is session.headers
    assert received["options"]["owner"] == admission.principal.owner_id
    assert received["options"]["delegated_credential"] is admission.principal.delegated
    authority = received["options"]["request_authority"]
    assert authority.bound_to(
        owner=admission.principal.owner_id, session_id="session-1",
    )
    assert authority.block_all
    assert authority.disable_mcp
    assert received["options"]["tool_policy"].block_all_tool_calls
    assert received["options"]["tool_policy"].disable_mcp
    assert received["options"]["turn_contract"] is None
    assert "bash" in received["options"]["disabled_tools"]
    assert ("prefs", admission.principal.owner_id) in ports.operations
    assert "preface" in ports.operations
    assert ports.preface_options["use_memory"] is False
    assert ports.preface_options["use_skills"] is False
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [
    "missing", "fake", "proof", "principal", "session", "loaded_owner", "ctx_owner",
])
async def test_invalid_admission_is_rejected_before_any_context_ports(tamper):
    admission, session, ports, kw = fixture()
    if tamper == "missing":
        admission = None
    elif tamper == "fake":
        admission = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        admission = replace(admission, _proof=None)
    elif tamper == "principal":
        admission = replace(admission, principal=Principal("mallory", "browser"))
    elif tamper == "session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "mallory"
    else:
        kw["context_owner"] = "mallory"
    with pytest.raises(AdmissionDenied) as exc:
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kw,
        ))
    assert exc.value.status_code == 403
    assert ports.operations == []


@pytest.mark.asyncio
async def test_account_agent_denial_precedes_any_preprocess_or_session_write():
    admission, session, ports, kw = fixture()
    kw["settings"] = TrustedTextTurnSettings(
        privileges={"can_use_agent": False},
    )
    with pytest.raises(AdmissionDenied) as denied:
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kw,
        ))
    assert denied.value.status_code == 403
    assert ports.operations == []
    assert session.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"request": None},
    {"request": TextTurnRequest("")},
    {"request": TextTurnRequest("  ")},
    {"request": TextTurnRequest(None)},
    {"ports": None},
    {"ports": object()},
    {"settings": object()},
    {"settings": TrustedTextTurnSettings(max_rounds=0)},
    {"settings": TrustedTextTurnSettings(max_rounds=True)},
    {"settings": TrustedTextTurnSettings(max_tokens=0)},
    {"settings": TrustedTextTurnSettings(global_disabled_tools=("bash", None))},
    {"settings": TrustedTextTurnSettings(privileges="admin")},
])
async def test_invalid_composition_input_fails_before_context_effects(bad):
    admission, session, ports, kw = fixture()
    kw.update(bad)
    with pytest.raises(AdmissionDenied):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **kw,
        ))
    assert ports.operations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "close", "error", "cancel"])
async def test_composed_stream_closes_inherited_loop_on_every_exit(
    monkeypatch, ending,
):
    admission, session, ports, kw = fixture()
    closed = []

    async def provider(*_a, **_kw):
        try:
            yield "first"
            if ending == "error":
                raise RuntimeError("provider error")
            if ending == "cancel":
                raise asyncio.CancelledError()
        finally:
            closed.append(True)

    monkeypatch.setattr(turn_module, "stream_agent_loop", provider)
    stream = GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **kw,
    )
    assert await anext(stream) == "first"
    if ending == "close":
        await stream.aclose()
    else:
        with pytest.raises({
            "complete": StopAsyncIteration,
            "error": RuntimeError,
            "cancel": asyncio.CancelledError,
        }[ending]):
            await anext(stream)
    await stream.aclose()
    assert closed == [True]



@pytest.mark.asyncio
async def test_explicit_shell_request_remains_tool_denied_and_never_written(monkeypatch):
    admission, session, ports, kwargs = fixture()
    kwargs["request"] = TextTurnRequest("Run bash to inspect the workspace")
    kwargs["settings"] = TrustedTextTurnSettings(
        global_disabled_tools=("web_search", "bash"),
    )
    seen = {}

    async def provider(_url, _model, messages, **options):
        seen["messages"] = list(messages)
        seen["authority"] = options["request_authority"]
        seen["policy"] = options["tool_policy"]
        yield "text only"

    monkeypatch.setattr(turn_module, "stream_agent_loop", provider)
    gateway = GatewayTurns(TurnService())
    assert await gather(gateway.stream_composed_text_turn(
        admission, session, **kwargs,
    )) == ["text only"]
    assert seen["messages"][-1]["content"] == kwargs["request"].message
    assert seen["authority"].block_all
    assert seen["authority"].disable_mcp
    assert seen["policy"].blocks("bash")
    assert seen["policy"].blocks("web_search")
    assert not session.messages
    assert "preface" in ports.operations


@pytest.mark.asyncio
async def test_repeated_non_persisting_turns_do_not_accumulate_session_history(
    monkeypatch,
):
    admission, session, ports, kwargs = fixture()
    sent = []

    async def provider(_url, _model, messages, **_options):
        sent.append([dict(item) for item in messages])
        yield "ok"

    monkeypatch.setattr(turn_module, "stream_agent_loop", provider)
    gateway = GatewayTurns(TurnService())
    for text in ("First independent request", "Second independent request"):
        kw = dict(kwargs, request=TextTurnRequest(text))
        assert await gather(gateway.stream_composed_text_turn(
            admission, session, **kw,
        )) == ["ok"]
    assert [items[-1]["content"] for items in sent] == [
        "First independent request", "Second independent request",
    ]
    assert all(
        sum(item.get("content") == "First independent request" for item in items) <= 1
        for items in sent
    )
    assert session.messages == []
    assert session.history == []


def test_composer_imports_no_web_or_http_and_declares_no_api():
    from src import gateway_turn_composer

    tree = ast.parse(inspect.getsource(gateway_turn_composer))
    for item in ast.walk(tree):
        if isinstance(item, ast.ImportFrom):
            assert not (item.module or "").startswith(("routes", "fastapi", "starlette"))
        elif isinstance(item, ast.Import):
            assert all(
                not name.name.startswith(("routes", "fastapi", "starlette"))
                for name in item.names
            )
    assert not hasattr(gateway_turn_composer, "router")
    assert not hasattr(gateway_turn_composer, "authenticate")
    assert "stream_agent_loop(" not in inspect.getsource(gateway_turn_composer)


def test_full_web_stream_retains_legacy_writers_until_cutover():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "routes/chat_routes.py"
    ).read_text()
    assert "gateway_turns.stream_admitted_agent(" in source
    assert "gateway_turns.stream_composed_text_turn(" not in source
    # Synthetic, non-persisting text composer is not ready to replace
    # the existing Web response finalization and approval pipeline.
    assert "stream_with_save()" in source
