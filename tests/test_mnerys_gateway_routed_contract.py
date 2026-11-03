"""M2.1 admitted handoff to the existing immutable routed tool contract."""

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_routed_contract import (
    AdmittedRoutedContract, resolve_admitted_routed_contract,
)
from src.gateway_turns import GatewayTurns
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.turn_contract import TurnContract


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search public results",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Read an exact page",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def issued(credential="browser", owner="alice", session_id="s1"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session_id, lambda _: SessionOwnership(owner_id=owner),
    )


def call(*, admission=None, session=None, context_owner="alice", request_authority=None,
         policy=None, capabilities=("search_browser",), schemas=SCHEMAS,
         required_tools=(), required_capabilities=None, selected_tools=None,
         always_available_tools=(), warm_tools=(), message="Search the web",
         history=()):
    admission = admission if admission is not None else issued()
    session = session if session is not None else SimpleNamespace(
        id=admission.session_id, owner=admission.principal.owner_id,
    )
    request_authority = (
        request_authority if request_authority is not None
        else RequestAuthority.empty(
            owner=admission.principal.owner_id, session_id=admission.session_id,
        )
    )
    return resolve_admitted_routed_contract(
        admission, session, context_owner=context_owner,
        request_authority=request_authority,
        capabilities=capabilities, schemas=schemas,
        policy=policy if policy is not None else ToolPolicy(),
        required_tools=required_tools, required_capabilities=required_capabilities,
        selected_tools=selected_tools,
        always_available_tools=always_available_tools,
        warm_tools=warm_tools, message=message, history=history,
    )


@pytest.mark.parametrize("credential", ["browser", "bearer"])
def test_valid_admitted_turn_uses_original_immutable_resolver(credential):
    grant = issued(credential)
    result = call(admission=grant)
    assert isinstance(result, AdmittedRoutedContract)
    assert isinstance(result.contract, TurnContract)
    assert result.contract.offered <= {"web_search", "web_fetch"}
    assert result.authority.owner == "alice"
    assert result.authority.session_id == "s1"
    assert result.authority.grants == ()
    # A schema/selection result never creates an executable operation grant.
    assert result.authority.grants == ()


def test_single_user_mode_preserves_anonymous_owner_binding():
    grant = issued("single_user")
    sess = SimpleNamespace(id="s1", owner=None)
    authority = RequestAuthority.empty(owner=None, session_id="s1")
    result = call(
        admission=grant, session=sess, context_owner=None,
        request_authority=authority,
    )
    assert result.authority.owner == ""
    assert result.authority.session_id == "s1"


def test_disabled_tool_cannot_be_reintroduced_by_selection_or_warm_state():
    policy = build_effective_tool_policy(disabled_tools={"web_search"})
    result = call(
        policy=policy, selected_tools={"web_search"},
        warm_tools={"web_search", "web_fetch"},
    )
    assert "web_search" not in result.contract.offered
    assert "web_search" in result.authority.denied
    assert not result.authority.grants


def test_guide_only_policy_is_restricted_without_minting_new_authority():
    policy = build_effective_tool_policy(
        last_user_message="Do not use any tools, just explain the steps.",
    )
    assert policy.block_all_tool_calls
    result = call(policy=policy)
    assert result.contract.offered == frozenset()
    assert result.authority.block_all is True
    assert result.authority.disable_mcp is True


def test_selected_required_and_warm_tools_are_forwarded_to_existing_resolver(monkeypatch):
    from src import gateway_routed_contract as service

    original = service.resolve_turn_contract
    invocations = []

    def capturing(**kwargs):
        invocations.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service, "resolve_turn_contract", capturing)
    result = call(
        capabilities={"search_browser"},
        selected_tools={"web_search"},
        required_tools={"web_search"},
        required_capabilities={"search_browser"},
        warm_tools={"web_fetch"},
        always_available_tools={"web_fetch"},
        history=[{"role": "user", "content": "Search."}],
    )
    assert isinstance(result.contract, TurnContract)
    assert len(invocations) == 1
    assert invocations[0]["selected_tools"] == {"web_search"}
    assert invocations[0]["required_tools"] == {"web_search"}
    assert invocations[0]["warm_tools"] == {"web_fetch"}
    assert invocations[0]["always_available_tools"] == {"web_fetch"}
    assert invocations[0]["history"] == [{"role": "user", "content": "Search."}]


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
    "authority_session", "authority_owner",
])
def test_no_resolver_invocation_for_invalid_admission_or_authority(monkeypatch, tamper):
    from src import gateway_routed_contract as service

    invoke = MagicMock()
    monkeypatch.setattr(service, "resolve_turn_contract", invoke)
    grant = issued()
    session = SimpleNamespace(id="s1", owner="alice")
    authority = RequestAuthority.empty(owner="alice", session_id="s1")
    owner = "alice"

    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "s1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session_id":
        grant = replace(grant, session_id="other")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "bob"
    elif tamper == "context_owner":
        owner = "bob"
    elif tamper == "authority_session":
        authority = RequestAuthority.empty(owner="alice", session_id="other")
    elif tamper == "authority_owner":
        authority = RequestAuthority.empty(owner="bob", session_id="s1")

    with pytest.raises(AdmissionDenied) as exc:
        resolve_admitted_routed_contract(
            grant, session, context_owner=owner,
            request_authority=authority, capabilities=("search_browser",),
            schemas=SCHEMAS, policy=ToolPolicy(),
        )
    assert exc.value.status_code == 403
    invoke.assert_not_called()


@pytest.mark.parametrize(("authority", "policy"), [
    (object(), ToolPolicy()),
    ("forged", ToolPolicy()),
    (RequestAuthority.empty(owner="alice", session_id="s1"), None),
    (RequestAuthority.empty(owner="alice", session_id="s1"), object()),
])
def test_invalid_trusted_policy_types_deny_before_resolution(authority, policy, monkeypatch):
    from src import gateway_routed_contract as service

    invoke = MagicMock()
    monkeypatch.setattr(service, "resolve_turn_contract", invoke)
    with pytest.raises(AdmissionDenied):
        service.resolve_admitted_routed_contract(
            issued(), SimpleNamespace(id="s1", owner="alice"),
            context_owner="alice", request_authority=authority,
            capabilities=(), schemas=SCHEMAS, policy=policy,
        )
    invoke.assert_not_called()


def test_gateway_facade_returns_identical_admitted_result():
    grant = issued()
    session = SimpleNamespace(id="s1", owner="alice")
    authority = RequestAuthority.empty(owner="alice", session_id="s1")
    args = dict(
        context_owner="alice", request_authority=authority,
        capabilities=("search_browser",), schemas=SCHEMAS, policy=ToolPolicy(),
        message="Search the web",
    )
    gateway = GatewayTurns(MagicMock())
    facade = gateway.resolve_routed_contract(grant, session, **args)
    direct = resolve_admitted_routed_contract(grant, session, **args)
    assert facade == direct


def test_core_service_has_no_web_or_http_imports():
    from src import gateway_routed_contract

    tree = ast.parse(inspect.getsource(gateway_routed_contract))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        if isinstance(node, ast.Import):
            assert all(
                not entry.name.startswith(("routes", "fastapi", "starlette"))
                for entry in node.names
            )


def test_web_route_uses_gateway_before_compact_preview_scoping():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "resolve_routed_contract"
    ]
    assert len(calls) == 1
    kw = {item.arg: item.value for item in calls[0].keywords}
    assert isinstance(kw["context_owner"], ast.Attribute)
    assert kw["context_owner"].attr == "user"
    assert isinstance(kw["request_authority"], ast.Name)
    assert kw["request_authority"].id == "_request_authority"
    assert isinstance(kw["policy"], ast.Name)
    assert kw["policy"].id == "_contract_policy"
    assert source.index("gateway_turns.resolve_routed_contract(") < source.index("gateway_turns.scope_routed_contract(")
    assert "scope_preview_contract(" not in source
    # The Web route must not bypass Gateway to call the resolver directly.
    assert not [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_turn_contract"
    ]
