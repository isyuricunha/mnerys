"""M2.1: compact scope and final denylist stay owner-bound and behavior-compatible."""

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
from src.gateway_contract_scoping import (
    AdmittedScopedContract, scope_admitted_routed_contract,
)
from src.gateway_routed_contract import resolve_admitted_routed_contract
from src.gateway_turns import GatewayTurns
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def grant(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "s1", lambda _: SessionOwnership(owner_id=owner),
    )


def setup(*, owner="alice", credential="browser", policy=None, selected=None,
          active=("notes",), disabled=(), model="regular-model", warm=(),
          message="Show my notes", **kwargs):
    admission = grant(owner, credential)
    session = SimpleNamespace(
        id="s1", owner=admission.principal.owner_id, model=model,
    )
    policy = policy or ToolPolicy()
    authority = RequestAuthority.empty(
        owner=admission.principal.owner_id, session_id="s1",
    )
    schemas = FUNCTION_TOOL_SCHEMAS
    routed = resolve_admitted_routed_contract(
        admission, session,
        context_owner=admission.principal.owner_id,
        request_authority=authority,
        capabilities=kwargs.get("active_capabilities", active),
        schemas=schemas,
        policy=policy,
        selected_tools=selected,
        warm_tools=warm,
        message=message,
        history=(),
    )
    params = dict(
        context_owner=admission.principal.owner_id,
        routed=routed,
        schemas=schemas,
        contract_policy=policy,
        disabled_tools=set(disabled),
        active_capabilities=frozenset(active),
        selected_tools=selected,
        warm_tools=frozenset(warm),
        compact_preview=False,
        native_workspace_contract=False,
        structured_document_intent=False,
        local_browser_render_intent=False,
        experiment_header=None,
        message=message,
        history=(),
        browser_requested=False,
    )
    params.update(kwargs)
    return admission, session, params


def old_route_reference(*, owner, model, **p):
    """Independent, test-only copy of the original Web scoping/reconciliation.

    This is a parity oracle, not an alternate production runtime.
    """
    from src.clean_agent_preview import (
        INTERACTIVE_CORE_TOOLS, MODE, NATIVE_WORKSPACE_TOOLS, PREVIEW_TOOLS,
        canonical, scope_preview_contract,
    )
    from src.tool_routing_experiment import experiment_mode, select_experiment_inventory
    from src.turn_contract import resolve_full_inventory_contract

    routed = p["routed"].contract
    schemas = list(p["schemas"])
    disabled = set(p["disabled_tools"])
    disabled.difference_update(routed.offered)
    contract = routed
    warm = p["warm_tools"]
    selected = p["selected_tools"]
    active = p["active_capabilities"]
    if p["compact_preview"]:
        warm_canonical = {canonical(name) for name in warm}
        clean_tools = PREVIEW_TOOLS | (
            NATIVE_WORKSPACE_TOOLS if p["native_workspace_contract"] else frozenset()
        )
        preview_schemas = [
            s for s in schemas if canonical(s["function"]["name"]) in clean_tools
        ]
        if p["native_workspace_contract"] and p["structured_document_intent"]:
            preview_schemas = [
                s for s in preview_schemas
                if canonical(s["function"]["name"]) != "bash"
                or canonical(s["function"]["name"]) in warm_canonical
            ]
        exact_native_chain = bool(
            selected and {"write_file", "read_file"}.issubset(selected)
            and set(selected).intersection({"inspect_media", "extract_text"})
            and set(selected).issubset(
                {"inspect_media", "extract_text", "write_file", "read_file"}
            )
        )
        extra = (
            frozenset()
            if exact_native_chain or active == frozenset({"tasks"}) or (
                active in (frozenset({"transcription"}), frozenset({"ocr"}))
                and not selected
            )
            else INTERACTIVE_CORE_TOOLS | warm | (
                NATIVE_WORKSPACE_TOOLS | (
                    {"private_browser"} if p["local_browser_render_intent"]
                    else frozenset()
                )
                if p["native_workspace_contract"] else frozenset()
            )
        )
        contract = scope_preview_contract(
            replace(
                resolve_full_inventory_contract(
                    schemas=preview_schemas, policy=p["contract_policy"],
                ),
                selection_mode=MODE,
            ),
            routed, active, extra_tools=extra,
        )
        mode = experiment_mode(p["experiment_header"], owner, model=model)
        if mode != "baseline":
            contract = select_experiment_inventory(
                replace(
                    resolve_full_inventory_contract(
                        schemas=[
                            s for s in schemas
                            if canonical(s["function"]["name"]) in clean_tools
                        ],
                        policy=p["contract_policy"],
                    ),
                    selection_mode=MODE,
                ),
                routed, p["history"], mode,
                user_text=p["message"], browser_requested=p["browser_requested"],
            )
    disabled.update(
        s["function"]["name"] for s in schemas
        if not contract.permits(s["function"]["name"])
    )
    disabled.difference_update(contract.offered)
    return contract, frozenset(disabled), build_effective_tool_policy(
        disabled_tools=disabled, last_user_message=p["message"],
    )


@pytest.mark.parametrize("case", [
    {},
    {"compact_preview": True},
    {"compact_preview": True, "disabled_tools": {"web_search", "bash"}},
    {
        "compact_preview": True, "native_workspace_contract": True,
        "active_capabilities": frozenset({"shell_files"}),
        "selected_tools": {"read_file", "write_file", "inspect_media"},
    },
    {
        "compact_preview": True, "native_workspace_contract": True,
        "active_capabilities": frozenset({"ocr"}),
        "structured_document_intent": True,
    },
    {
        "compact_preview": True, "native_workspace_contract": True,
        "active_capabilities": frozenset({"ocr"}),
        "structured_document_intent": True, "warm_tools": frozenset({"bash"}),
    },
    {
        "compact_preview": True, "native_workspace_contract": True,
        "active_capabilities": frozenset({"search_browser"}),
        "local_browser_render_intent": True,
        "browser_requested": True,
    },
    {"compact_preview": True, "active_capabilities": frozenset({"tasks"})},
    {"compact_preview": True, "active_capabilities": frozenset({"transcription"})},
    {
        "compact_preview": True, "experiment_header": "recent",
        "active_capabilities": frozenset({"notes"}),
    },
])
def test_scoped_contract_matches_inherited_route_for_all_inventory_variants(case):
    owner = "sft_alex_creator" if case.get("experiment_header") else "alice"
    admission, session, params = setup(owner=owner, **case)
    original_disabled = set(params["disabled_tools"])
    result = scope_admitted_routed_contract(admission, session, **params)
    expected_contract, expected_disabled, expected_policy = old_route_reference(
        owner=admission.principal.owner_id, model=session.model, **params,
    )
    assert isinstance(result, AdmittedScopedContract)
    assert result.contract == expected_contract
    assert result.disabled_tools == expected_disabled
    assert result.policy == expected_policy
    assert params["disabled_tools"] == original_disabled
    assert result.authority is params["routed"].authority
    assert params["routed"].contract.offered <= params["routed"].contract.executable


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
def test_admitted_principal_projections_share_one_compact_service(credential):
    admission, session, params = setup(credential=credential, compact_preview=True)
    result = GatewayTurns(MagicMock()).scope_routed_contract(
        admission, session, **params,
    )
    assert result.contract.offered
    assert result.authority.owner == (admission.principal.owner_id or "").casefold()


def test_compact_experiment_is_scoped_to_the_admitted_owner(monkeypatch):
    from src import tool_routing_experiment

    original = tool_routing_experiment.experiment_mode
    seen = []

    def audited(header, owner, model=None):
        seen.append((header, owner, model))
        return original(header, owner, model=model)

    monkeypatch.setattr(tool_routing_experiment, "experiment_mode", audited)

    admission, session, params = setup(
        compact_preview=True, experiment_header="recent",
    )
    result = scope_admitted_routed_contract(admission, session, **params)
    assert seen == [("recent", "alice", "regular-model")]
    assert result.contract.routing_experiment == "baseline"

    admission, session, params = setup(
        owner="sft_alex_creator", compact_preview=True,
        experiment_header="recent",
    )
    result = scope_admitted_routed_contract(admission, session, **params)
    assert seen[-1] == ("recent", "sft_alex_creator", "regular-model")
    assert result.contract.routing_experiment == "recent"


def test_non_compact_turn_never_evaluates_optional_experiment(monkeypatch):
    from src import tool_routing_experiment

    monkeypatch.setattr(
        tool_routing_experiment, "experiment_mode",
        lambda *_args, **_kwargs: pytest.fail("Experiment selector invoked"),
    )
    admission, session, params = setup(
        compact_preview=False, experiment_header="all",
    )
    result = scope_admitted_routed_contract(admission, session, **params)
    assert result.contract is params["routed"].contract


def test_global_denial_and_guide_only_never_reenabled_by_scoping():
    policy = build_effective_tool_policy(
        disabled_tools={"bash", "web_search"},
        last_user_message="Do not use any tools, just explain.",
    )
    admission, session, params = setup(
        policy=policy, compact_preview=True,
        disabled_tools={"bash", "web_search"},
        message="Do not use any tools, just explain.",
    )
    result = scope_admitted_routed_contract(admission, session, **params)
    assert not result.contract.offered
    assert result.policy.block_all_tool_calls is True
    assert result.policy.disable_mcp is True
    assert result.contract.offered.isdisjoint({"bash", "web_search"})


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session", "principal",
    "loaded_session", "loaded_owner", "context_owner",
    "authority_owner", "authority_session", "routed_type",
])
def test_forged_admission_or_routed_authority_rejected_before_scoping(tamper):
    admission, session, params = setup(compact_preview=True)
    if tamper == "missing":
        admission = None
    elif tamper == "fabricated":
        admission = AdmittedSession(Principal("alice", "browser"), "s1")
    elif tamper == "proof":
        admission = replace(admission, _proof=None)
    elif tamper == "session":
        admission = replace(admission, session_id="other")
    elif tamper == "principal":
        admission = replace(admission, principal=Principal("bob", "browser"))
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "bob"
    elif tamper == "context_owner":
        params["context_owner"] = "bob"
    elif tamper == "authority_owner":
        params["routed"] = replace(
            params["routed"],
            authority=RequestAuthority.empty(owner="bob", session_id="s1"),
        )
    elif tamper == "authority_session":
        params["routed"] = replace(
            params["routed"],
            authority=RequestAuthority.empty(owner="alice", session_id="other"),
        )
    elif tamper == "routed_type":
        params["routed"] = object()

    with pytest.raises(AdmissionDenied) as raised:
        scope_admitted_routed_contract(admission, session, **params)
    assert raised.value.status_code == 403


@pytest.mark.parametrize("flag,value", [
    ("compact_preview", "true"),
    ("native_workspace_contract", 1),
    ("structured_document_intent", None),
    ("local_browser_render_intent", "yes"),
    ("browser_requested", 1),
    ("experiment_header", []),
    ("message", 123),
    ("contract_policy", None),
    ("disabled_tools", ["bash"]),
])
def test_malformed_trusted_scope_context_denied(flag, value):
    admission, session, params = setup()
    params[flag] = value
    with pytest.raises(AdmissionDenied):
        scope_admitted_routed_contract(admission, session, **params)



@pytest.mark.parametrize("tamper", [
    "added_denial", "missing_authority_denial", "missing_block_all",
    "missing_mcp_denial",
])
def test_policy_inconsistent_contract_or_authority_fails_closed(tamper):
    admission, session, params = setup(
        active=("search_browser",), message="Search public sources",
    )
    routed = params["routed"]
    if tamper == "added_denial":
        # This policy would have blocked an offered tool during resolution.
        assert "web_search" in routed.contract.offered
        params["contract_policy"] = build_effective_tool_policy(
            disabled_tools={"web_search"},
        )
    elif tamper == "missing_authority_denial":
        params["contract_policy"] = build_effective_tool_policy(
            disabled_tools={"bash"},
        )
    elif tamper == "missing_block_all":
        params["contract_policy"] = build_effective_tool_policy(
            last_user_message="Do not use any tools; explain only.",
        )
    else:
        params["contract_policy"] = ToolPolicy(disable_mcp=True)

    with pytest.raises(AdmissionDenied) as raised:
        scope_admitted_routed_contract(admission, session, **params)
    assert raised.value.status_code == 403


def test_core_scoping_has_no_web_transport_imports():
    from src import gateway_contract_scoping

    tree = ast.parse(inspect.getsource(gateway_contract_scoping))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        if isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith(("routes", "fastapi", "starlette"))
                for alias in node.names
            )


def test_web_delegates_scope_once_after_resolving_routed_contract():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = ("resolve_routed_contract", "scope_routed_contract")
    calls = {
        name: [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "gateway_turns"
            and n.func.attr == name
        ]
        for name in methods
    }
    assert all(len(v) == 1 for v in calls.values())
    assert calls["resolve_routed_contract"][0].lineno < calls["scope_routed_contract"][0].lineno
    assert "scope_preview_contract(" not in source
    assert "select_experiment_inventory(" not in source
    assert "disabled_tools.difference_update(_turn_contract.offered)" not in source
    scope_call = calls["scope_routed_contract"][0]
    kw = {argument.arg: argument.value for argument in scope_call.keywords}
    assert isinstance(kw["context_owner"], ast.Attribute)
    assert kw["context_owner"].attr == "user"
    assert isinstance(kw["routed"], ast.Name)
    assert kw["routed"].id == "_resolved_routed_contract"
