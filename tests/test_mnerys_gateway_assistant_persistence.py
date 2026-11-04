"""Admitted assistant final-response save preserves the inherited writer.

The test writer records calls without touching any real database or history.
Web owns the existing saver, while Gateway validates session and owner before
delegating; no new writer, transaction or message serialization is introduced.
"""

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_assistant_persistence import save_admitted_assistant_response
from src.gateway_turns import GatewayTurns


def admitted(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential, frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    token = GatewayAdmission().admit(
        principal, "session-1", lambda _: SessionOwnership(owner_id=owner),
    )
    return token, SimpleNamespace(id="session-1", owner=principal.owner_id)


def inputs(**changes):
    token, session = admitted()
    saver = MagicMock(return_value="stored-message-1")
    manager = object()
    kw = dict(
        context_owner="alice",
        saver=saver,
        full_response="Final visible response",
        last_metrics={"model": "provider/test", "tokens": 42},
        character_name="Nery",
        web_sources=[{"url": "https://example.com"}],
        rag_sources=[{"id": "doc"}],
        research_sources=[],
        used_memories=[],
        do_research=False,
        tool_events=[{"type": "tool_result"}],
        incognito=False,
    )
    kw.update(changes)
    return token, session, manager, kw


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
@pytest.mark.parametrize("mode", ["success", "failed", "incognito"])
def test_legacy_saver_receives_exact_session_payload_and_returned_id(
    credential, mode,
):
    token, session = admitted(credential=credential)
    manager = object()
    saver = MagicMock(return_value=None if mode == "incognito" else "message-123")
    metadata = {"model": "fallback-model", "failed": mode == "failed"}
    result = GatewayTurns(MagicMock()).save_assistant_response(
        token, session, manager, token.session_id,
        context_owner=token.principal.owner_id,
        saver=saver,
        full_response="Reply",
        last_metrics=metadata,
        web_sources=[{"url": "https://example.com"}],
        incognito=(mode == "incognito"),
    )
    assert result == (None if mode == "incognito" else "message-123")
    saver.assert_called_once_with(
        session, manager, token.session_id, "Reply", metadata,
        character_name=None,
        web_sources=[{"url": "https://example.com"}],
        rag_sources=None, research_sources=None, used_memories=None,
        do_research=False, tool_events=None, incognito=(mode == "incognito"),
    )
    assert metadata == {"model": "fallback-model", "failed": mode == "failed"}


def test_source_passes_through_legacy_writer_without_reformatting():
    token, session, manager, kw = inputs()
    result = save_admitted_assistant_response(
        token, session, manager, token.session_id, **kw,
    )
    assert result == "stored-message-1"
    kw["saver"].assert_called_once_with(
        session, manager, token.session_id, kw["full_response"], kw["last_metrics"],
        character_name=kw["character_name"],
        web_sources=kw["web_sources"],
        rag_sources=kw["rag_sources"],
        research_sources=kw["research_sources"],
        used_memories=kw["used_memories"],
        do_research=kw["do_research"],
        tool_events=kw["tool_events"],
        incognito=kw["incognito"],
    )


@pytest.mark.parametrize("tamper", [
    "missing", "fake", "proof", "principal", "session_id", "loaded_session",
    "loaded_owner", "ctx_owner", "claimed_id",
])
def test_foreign_or_forged_session_never_reaches_writer(tamper):
    token, session, manager, kw = inputs()
    claimed_id = token.session_id
    if tamper == "missing":
        token = None
    elif tamper == "fake":
        token = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        token = replace(token, _proof=None)
    elif tamper == "principal":
        token = replace(token, principal=Principal("mallory", "browser"))
    elif tamper == "session_id":
        token = replace(token, session_id="other")
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "mallory"
    elif tamper == "ctx_owner":
        kw["context_owner"] = "mallory"
    elif tamper == "claimed_id":
        claimed_id = "foreign"
    with pytest.raises(AdmissionDenied) as denied:
        save_admitted_assistant_response(token, session, manager, claimed_id, **kw)
    assert denied.value.status_code == 403
    kw["saver"].assert_not_called()


@pytest.mark.parametrize("bad", [
    {"saver": None},
    {"saver": object()},
    {"full_response": None},
    {"last_metrics": []},
    {"character_name": 5},
    {"web_sources": "https://example.com"},
    {"rag_sources": {"doc": True}},
    {"research_sources": "sources"},
    {"used_memories": {"private": "wrong"}},
    {"tool_events": {"type": "test"}},
    {"do_research": 1},
    {"incognito": "false"},
])
def test_invalid_internal_save_input_fails_closed(bad):
    token, session, manager, kw = inputs()
    kw.update(bad)
    with pytest.raises(AdmissionDenied):
        save_admitted_assistant_response(
            token, session, manager, token.session_id, **kw,
        )
    if isinstance(kw["saver"], MagicMock):
        kw["saver"].assert_not_called()


def test_missing_manager_denied_before_legacy_writer():
    token, session, _, kw = inputs()
    with pytest.raises(AdmissionDenied):
        save_admitted_assistant_response(
            token, session, None, token.session_id, **kw,
        )
    kw["saver"].assert_not_called()


def test_web_handoff_uses_gateway_for_all_five_terminal_save_branches():
    source=(Path(__file__).resolve().parents[1]/"routes"/"chat_routes.py").read_text()
    assert source.count("gateway_turns.save_assistant_response(") == 5
    assert "_saved_id = save_assistant_response(" not in source
    stream=source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.index("_admitted_session = _verify_session_owner(") < stream.index(
        "gateway_turns.save_assistant_response("
    )
    tree=ast.parse(source)
    calls=[
        n for n in ast.walk(tree)
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
        and isinstance(n.func.value,ast.Name) and n.func.value.id=="gateway_turns"
        and n.func.attr=="save_assistant_response"
    ]
    assert len(calls)==5
    for call in calls:
        assert len(call.args)==6
        assert isinstance(call.args[0],ast.Name)
        assert call.args[0].id=="_admitted_session"
        assert isinstance(call.args[1],ast.Name) and call.args[1].id=="sess"
        assert isinstance(call.args[2],ast.Name) and call.args[2].id=="session_manager"
        assert isinstance(call.args[3],ast.Name) and call.args[3].id=="session"
        kw={x.arg:x.value for x in call.keywords}
        assert isinstance(kw["context_owner"],ast.Attribute)
        assert kw["context_owner"].attr=="user"
        assert isinstance(kw["saver"],ast.Name)
        assert kw["saver"].id=="save_assistant_response"


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
def test_interrupted_partial_preserves_legacy_writer_and_stopped_metadata(credential):
    from src.gateway_assistant_persistence import (
        save_admitted_interrupted_assistant_response,
    )

    token, session = admitted(credential=credential)
    manager = object()
    writer = MagicMock(return_value=None)
    metadata = {
        "stopped": True,
        "model": "actual-model",
        "requested_model": "selected-model",
        "round_models": ["selected-model", "actual-model"],
        "thinking_interrupted": True,
    }
    result = GatewayTurns(MagicMock()).save_interrupted_assistant_response(
        token, session, manager, token.session_id,
        "Partial answer", metadata,
        context_owner=token.principal.owner_id,
        writer=writer,
        incognito=False,
    )
    assert result is None
    writer.assert_called_once_with(session, manager, "Partial answer", metadata)
    assert metadata["stopped"] is True
    writer.reset_mock()
    assert save_admitted_interrupted_assistant_response(
        token, session, manager, token.session_id,
        "Partial answer", metadata,
        context_owner=token.principal.owner_id,
        writer=writer, incognito=True,
    ) is None
    writer.assert_not_called()


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "principal", "id",
    "loaded_session", "loaded_owner", "context_owner", "claimed_id",
])
def test_interrupted_save_rejects_foreign_admission_before_legacy_writer(tamper):
    from src.gateway_assistant_persistence import (
        save_admitted_interrupted_assistant_response,
    )
    token, session = admitted()
    manager = object()
    writer = MagicMock()
    owner = "alice"
    claimed = token.session_id
    if tamper == "missing":
        token = None
    elif tamper == "fabricated":
        token = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        token = replace(token, _proof=None)
    elif tamper == "principal":
        token = replace(token, principal=Principal("mallory", "browser"))
    elif tamper == "id":
        token = replace(token, session_id="other")
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "mallory"
    elif tamper == "context_owner":
        owner = "mallory"
    else:
        claimed = "other"
    with pytest.raises(AdmissionDenied) as denied:
        save_admitted_interrupted_assistant_response(
            token, session, manager, claimed, "Partial",
            {"stopped": True}, context_owner=owner, writer=writer,
        )
    assert denied.value.status_code == 403
    writer.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"stopped_metadata": None},
    {"stopped_metadata": {}},
    {"stopped_metadata": {"stopped": "true"}},
    {"stopped_metadata": {"stopped": False}},
    {"partial_content": None},
    {"writer": None},
    {"writer": object()},
    {"incognito": "false"},
])
def test_interrupted_save_invalid_internal_contract_fails_closed(changes):
    from src.gateway_assistant_persistence import (
        save_admitted_interrupted_assistant_response,
    )
    token, session = admitted()
    manager = object()
    writer = MagicMock()
    kw = dict(
        partial_content="Partial", stopped_metadata={"stopped": True},
        writer=writer, context_owner="alice", incognito=False,
    )
    kw.update(changes)
    with pytest.raises(AdmissionDenied):
        save_admitted_interrupted_assistant_response(
            token, session, manager, token.session_id, **kw,
        )
    writer.assert_not_called()


def test_web_cancel_paths_are_admission_bound_and_save_success_once():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text()
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.count("gateway_turns.save_interrupted_assistant_response(") == 2
    assert stream.count("writer=append_interrupted_assistant_response") == 2
    assert "if full_response and not incognito and not _chat_terminal_saved:" in stream
    assert "if full_response and not incognito and not _terminal_saved:" in stream
    assert stream.count("_chat_terminal_saved = True") == 2
    assert stream.count("_terminal_saved = True") >= 3
    assert "sess.add_message(ChatMessage(\"assistant\", _stopped_content" not in stream
    tree=ast.parse(source)
    paths=[
        x for x in ast.walk(tree)
        if isinstance(x, ast.Call)
        and isinstance(x.func, ast.Attribute)
        and isinstance(x.func.value, ast.Name)
        and x.func.value.id == "gateway_turns"
        and x.func.attr == "save_interrupted_assistant_response"
    ]
    assert len(paths) == 2
    for call in paths:
        assert [a.id for a in call.args[:4]] == [
            "_admitted_session", "sess", "session_manager", "session",
        ]
        kws={kw.arg:kw.value for kw in call.keywords}
        assert isinstance(kws["context_owner"],ast.Attribute)
        assert isinstance(kws["writer"],ast.Name)
        assert kws["writer"].id == "append_interrupted_assistant_response"


def test_service_imports_no_web_or_duplicate_database_writer():
    from src import gateway_assistant_persistence as module
    tree=ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("routes", "fastapi", "starlette", "core.database")
            )
        if isinstance(node,ast.Import):
            assert all(not n.name.startswith(
                ("routes", "fastapi", "starlette", "core.database")
            ) for n in node.names)
    assert "return saver(" in inspect.getsource(module)
