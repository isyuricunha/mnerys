"""M2.1 admitted runtime toggle context projection parity and denial tests."""

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
from src.gateway_turn_runtime_state import (
    AdmittedRuntimeUIState, project_admitted_runtime_ui_state,
)
from src.gateway_turns import GatewayTurns
from src.tool_policy import build_effective_tool_policy


def admitted(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential, frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    grant = GatewayAdmission().admit(
        principal, "session-1",
        lambda _: SessionOwnership(owner_id=owner),
    )
    return grant, SimpleNamespace(id="session-1", owner=principal.owner_id)


def options(*, disabled=(), client=None, use_rag=None, use_research=None, incognito=False):
    return dict(
        context_owner="alice",
        client_runtime_context=client,
        policy=build_effective_tool_policy(
            disabled_tools=set(disabled), last_user_message="Hello",
        ),
        use_rag=use_rag,
        use_research=use_research,
        incognito=incognito,
    )


def previous_route_reference(params):
    """Independent exact snapshot of the original Web-side toggle projection."""
    context = dict(params["client_runtime_context"] or {})
    disabled = params["policy"].all_disabled_names()
    context["web_ui_state"] = {
        "web": "web_search" not in disabled,
        "bash": "bash" not in disabled,
        "rag": str(
            params["use_rag"] if params["use_rag"] is not None else "true"
        ).lower() != "false",
        "research": str(params["use_research"] or "").lower() == "true",
        "incognito": bool(params["incognito"]),
        "document_editor": not {
            "manage_documents", "create_document", "edit_document", "update_document",
        }.issubset(disabled),
    }
    return context


@pytest.mark.parametrize("case", [
    {},
    {"disabled": ("web_search",)},
    {"disabled": ("web_search", "web_fetch", "private_browser")},
    {"disabled": ("bash",)},
    {"disabled": ("web_search", "bash")},
    {"disabled": ("manage_documents", "create_document", "edit_document", "update_document")},
    {"disabled": ("create_document", "edit_document", "update_document")},
    {"disabled": ("manage_documents", "create_document", "edit_document", "update_document", "web_search")},
    {"client": {"surface": "odysseus-tui", "turn_controls": {"web": True}}},
    {"client": {"surface": "odysseus-native", "web_ui_state": {"web": True, "bash": True}}},
    {"client": {"web_ui_state": {"web": False}, "custom": {"hello": True}}},
    {"client": {}},
    {"use_rag": "false"},
    {"use_rag": "FALSE"},
    {"use_rag": False},
    {"use_rag": True},
    {"use_rag": "off"},
    {"use_research": "true"},
    {"use_research": "TRUE"},
    {"use_research": True},
    {"use_research": False},
    {"use_research": "on"},
    {"incognito": True},
    {"incognito": True, "use_research": "true", "use_rag": "false"},
])
def test_projection_matches_original_web_toggle_semantics(case):
    grant, session = admitted()
    kw = options(**case)
    expected = previous_route_reference(kw)
    previous_client = dict(kw["client_runtime_context"] or {})
    result = project_admitted_runtime_ui_state(grant, session, **kw)
    assert isinstance(result, AdmittedRuntimeUIState)
    assert result.context == expected
    assert result.web_ui_state == expected["web_ui_state"]
    assert kw["client_runtime_context"] is None or kw["client_runtime_context"] == previous_client
    assert set(result.web_ui_state) == {
        "web", "bash", "rag", "research", "incognito", "document_editor",
    }
    assert all(type(v) is bool for v in result.web_ui_state.values())


@pytest.mark.parametrize("kind", ["browser", "bearer", "single_user"])
def test_all_admitted_credentials_use_the_same_server_policy(kind):
    grant, session = admitted(credential=kind)
    kw = options(
        disabled={"web_search", "bash"},
        client={"web_ui_state": {"web": True, "bash": True}},
    )
    if kind == "single_user":
        kw["context_owner"] = None
    result = GatewayTurns(MagicMock()).project_runtime_ui_state(
        grant, session, **kw,
    )
    assert result == project_admitted_runtime_ui_state(grant, session, **kw)
    assert result.web_ui_state["web"] is False
    assert result.web_ui_state["bash"] is False


def test_client_cannot_spoof_or_restore_disabled_tool_availability():
    grant, session = admitted()
    client = {
        "web_ui_state": {
            "web": True, "bash": True, "document_editor": True, "research": True,
        },
        "surface": "odysseus-tui",
    }
    kw = options(
        disabled={
            "web_search", "bash", "manage_documents", "create_document",
            "edit_document", "update_document",
        },
        client=client,
    )
    result = project_admitted_runtime_ui_state(grant, session, **kw)
    assert not result.web_ui_state["web"]
    assert not result.web_ui_state["bash"]
    assert not result.web_ui_state["document_editor"]
    assert result.context["surface"] == "odysseus-tui"
    assert client["web_ui_state"]["web"] is True


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
])
def test_wrong_owner_session_or_proof_fails_before_context_projection(tamper):
    grant, session = admitted()
    kw = options(client={"web_ui_state": {"web": True}})
    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session_id":
        grant = replace(grant, session_id="other")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("other", "browser"))
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "other"
    elif tamper == "context_owner":
        kw["context_owner"] = "other"
    with pytest.raises(AdmissionDenied) as denied:
        project_admitted_runtime_ui_state(grant, session, **kw)
    assert denied.value.status_code == 403
    assert kw["client_runtime_context"] == {"web_ui_state": {"web": True}}


@pytest.mark.parametrize("bad", [
    {"client_runtime_context": "untrusted"},
    {"client_runtime_context": [("web_ui_state", {"web": True})]},
    {"client_runtime_context": object()},
    {"incognito": 1},
    {"incognito": "false"},
    {"policy": None},
    {"policy": object()},
])
def test_bad_internal_inputs_fail_closed(bad):
    grant, session = admitted()
    kw = options()
    kw.update(bad)
    with pytest.raises(AdmissionDenied):
        project_admitted_runtime_ui_state(grant, session, **kw)


def test_gateway_has_no_transport_or_tool_executor_dependencies():
    from src import gateway_turn_runtime_state
    tree = ast.parse(inspect.getsource(gateway_turn_runtime_state))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("routes", "fastapi", "starlette", "src.tool_execution")
            )
        if isinstance(node, ast.Import):
            assert all(
                not import_node.name.startswith(
                    ("routes", "fastapi", "starlette", "src.tool_execution")
                )
                for import_node in node.names
            )


def test_route_projects_only_after_final_policy_and_before_request_authority():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text()
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.count("gateway_turns.project_runtime_ui_state(") == 1
    assert stream.index("gateway_turns.finalize_tool_policy(") < stream.index(
        "gateway_turns.project_runtime_ui_state("
    ) < stream.index("gateway_turns.build_authority(")
    assert "policy=tool_policy," in stream
    assert "client_runtime_context = _runtime_ui_projection.context" in stream
    assert 'client_runtime_context["web_ui_state"]' not in stream
