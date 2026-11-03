"""M2.1 admitted contract/runtime path selection, without a second tool engine."""

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
    SessionOwnership,
)
from src.gateway_turn_contract_selection import (
    AdmittedContractScope,
    select_admitted_compact_runtime,
    select_admitted_contract_scope,
    turn_contract_enabled,
)
from src.gateway_turns import GatewayTurns


def issued(credential="browser", *, owner="alice", session_id="s1"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session_id,
        lambda _sid: SessionOwnership(owner_id=owner),
    )


def session_for(admission):
    return SimpleNamespace(
        id=admission.session_id,
        owner=admission.principal.owner_id,
    )


def prepare(
    admission=None, session=None, *,
    context_owner="alice", clean_route_requested=True,
    full_schema_route=False, exact_tool_approval=None, runtime_surface="",
    agent_mode=True, agent_permitted=True, image_generation=False,
):
    admission = admission if admission is not None else issued()
    session = session if session is not None else session_for(admission)
    return select_admitted_compact_runtime(
        admission, session, context_owner=context_owner,
        clean_route_requested=clean_route_requested,
        full_schema_route=full_schema_route,
        exact_tool_approval=exact_tool_approval,
        runtime_surface=runtime_surface, agent_mode=agent_mode,
        agent_permitted=agent_permitted, image_generation=image_generation,
    )


def scope(
    admission=None, session=None, *,
    context_owner="alice", client_runtime_context=None,
    workspace_agent_intent=False, workspace_bound=False,
    clean_route_requested=False, full_schema_route=False,
    exact_tool_approval=None,
):
    admission = admission if admission is not None else issued()
    session = session if session is not None else session_for(admission)
    return select_admitted_contract_scope(
        admission, session, context_owner=context_owner,
        client_runtime_context=client_runtime_context,
        workspace_agent_intent=workspace_agent_intent,
        workspace_bound=workspace_bound,
        clean_route_requested=clean_route_requested,
        full_schema_route=full_schema_route,
        exact_tool_approval=exact_tool_approval,
    )


@pytest.mark.parametrize("credential", ["browser", "bearer"])
def test_admitted_browser_and_bearer_share_compact_route(credential):
    grant = issued(credential)
    assert prepare(grant, session_for(grant)) is True
    result = scope(grant, session_for(grant), clean_route_requested=True)
    assert result == AdmittedContractScope(True, False)


def test_single_user_projection_can_select_without_forged_owner():
    grant = issued("single_user")
    assert prepare(grant, session_for(grant), context_owner=None) is True
    assert scope(grant, session_for(grant), context_owner=None).enabled is True


@pytest.mark.parametrize(("override", "expected"), [
    ({"clean_route_requested": False}, False),
    ({"full_schema_route": True}, False),
    ({"exact_tool_approval": object()}, False),
    ({"runtime_surface": "odysseus-tui"}, False),
    ({"agent_mode": False}, False),
    ({"agent_permitted": False}, False),
    ({"image_generation": True}, False),
])
def test_compact_pre_context_gates_stay_inherited(override, expected):
    assert prepare(**override) is expected


def test_clean_route_preparation_is_not_vetoed_by_native_workspace_selection():
    assert prepare(runtime_surface="odysseus-native") is True
    result = scope(
        clean_route_requested=True,
        client_runtime_context={
            "surface": "odysseus-native", "terminal_agent": True,
            "unattended_mode": True,
        },
        workspace_bound=True,
    )
    assert result == AdmittedContractScope(True, True)


@pytest.mark.parametrize(("context", "workspace_intent", "workspace_bound", "native"), [
    ({}, False, False, False),
    ({"surface": "odysseus-native"}, True, True, False),
    ({"surface": "odysseus-native", "terminal_agent": True}, False, False, False),
    ({"surface": "odysseus-native", "terminal_agent": "true"}, True, False, False),
    ({"surface": "odysseus-tui", "terminal_agent": True}, True, True, False),
    ({"surface": "odysseus-native", "terminal_agent": True}, True, False, True),
    ({"surface": "odysseus-native", "terminal_agent": True, "unattended_mode": True}, False, False, False),
    ({"surface": "odysseus-native", "terminal_agent": True, "unattended_mode": True}, False, True, True),
    ({"surface": "odysseus-native", "terminal_agent": True, "unattended_mode": "true"}, False, True, False),
])
def test_native_marker_alone_never_confers_workspace_scope(
    context, workspace_intent, workspace_bound, native,
):
    result = scope(
        client_runtime_context=context,
        workspace_agent_intent=workspace_intent,
        workspace_bound=workspace_bound,
    )
    assert result.native_workspace_contract is native
    assert result.enabled is (
        not native and context.get("surface") != "odysseus-tui"
    )


def test_native_workspace_can_use_clean_route_contract_when_requested():
    result = scope(
        clean_route_requested=True,
        workspace_agent_intent=True,
        client_runtime_context={"surface": "odysseus-native", "terminal_agent": True},
    )
    assert result == AdmittedContractScope(True, True)


@pytest.mark.parametrize("override", [
    {"client_runtime_context": {"surface": "odysseus-tui"}},
    {"full_schema_route": True},
    {"exact_tool_approval": object()},
])
def test_late_contract_exclusions_keep_full_inventory_or_approval(override):
    assert scope(**override).enabled is False


@pytest.mark.parametrize("override", [
    {"clean_route_requested": False},
    {"clean_route_requested": True},
])
def test_regular_web_route_uses_contract_without_native_workspace(override):
    result = scope(**override)
    assert result == AdmittedContractScope(True, False)


def test_original_contract_rule_stays_importable_for_legacy_route():
    from routes.chat_routes import _turn_contract_enabled

    assert _turn_contract_enabled is turn_contract_enabled
    assert _turn_contract_enabled(
        exact_tool_approval=None,
        runtime_surface="odysseus-native",
        native_workspace_contract=True,
        clean_v3_route=False,
    ) is False


@pytest.mark.parametrize("tamper", [
    "none", "fabricated", "proof", "session", "principal", "loaded_owner",
    "loaded_session", "context",
])
def test_tampered_admission_denied_before_selection(tamper):
    grant = issued()
    sess = session_for(grant)
    owner = "alice"
    if tamper == "none":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "s1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session":
        grant = replace(grant, session_id="s2")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded_owner":
        sess.owner = "bob"
    elif tamper == "loaded_session":
        sess.id = "s2"
    elif tamper == "context":
        owner = "bob"
    for selector in (
        lambda: select_admitted_compact_runtime(
            grant, sess, context_owner=owner,
            clean_route_requested=True, full_schema_route=False,
            exact_tool_approval=None, runtime_surface="", agent_mode=True,
            agent_permitted=True, image_generation=False,
        ),
        lambda: select_admitted_contract_scope(
            grant, sess, context_owner=owner,
            client_runtime_context={},
            workspace_agent_intent=False, workspace_bound=False,
            clean_route_requested=False, full_schema_route=False,
            exact_tool_approval=None,
        ),
    ):
        with pytest.raises(AdmissionDenied) as exc:
            selector()
        assert exc.value.status_code == 403


@pytest.mark.parametrize(("selector", "override"), [
    ("early", {"agent_mode": 1}),
    ("early", {"agent_permitted": "yes"}),
    ("early", {"full_schema_route": None}),
    ("early", {"runtime_surface": 123}),
    ("late", {"workspace_agent_intent": 1}),
    ("late", {"workspace_bound": "yes"}),
    ("late", {"full_schema_route": None}),
    ("late", {"client_runtime_context": "forged"}),
])
def test_invalid_server_resolution_flags_fail_closed(selector, override):
    with pytest.raises(AdmissionDenied):
        if selector == "early":
            prepare(**override)
        else:
            scope(**override)


def test_gateway_facade_delegates_both_admitted_selections():
    grant = issued()
    sess = session_for(grant)
    gateway = GatewayTurns(MagicMock())
    assert gateway.select_compact_runtime(
        grant, sess, context_owner="alice",
        clean_route_requested=True, full_schema_route=False,
        exact_tool_approval=None, runtime_surface="",
        agent_mode=True, agent_permitted=True, image_generation=False,
    )
    assert gateway.select_contract_scope(
        grant, sess, context_owner="alice",
        client_runtime_context=None,
        workspace_agent_intent=False, workspace_bound=False,
        clean_route_requested=False, full_schema_route=False,
        exact_tool_approval=None,
    ) == AdmittedContractScope(True, False)


def test_contract_selection_has_no_transport_imports():
    from src import gateway_turn_contract_selection

    tree = ast.parse(inspect.getsource(gateway_turn_contract_selection))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        if isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith(("routes", "fastapi", "starlette"))
                for alias in node.names
            )


def test_web_adapter_selects_pre_and_post_context_through_gateway():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text()
    tree = ast.parse(source)
    for name in ("select_compact_runtime", "select_contract_scope"):
        matches = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "gateway_turns"
            and node.func.attr == name
        ]
        assert len(matches) == 1
    start = source.index('@router.post("/api/chat_stream")')
    stream = source[start:]
    assert stream.index("gateway_turns.select_compact_runtime(") < stream.index("build_chat_context(")
    assert stream.index("build_chat_context(") < stream.index("gateway_turns.select_contract_scope(")
