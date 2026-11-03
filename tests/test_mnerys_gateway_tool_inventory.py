"""M2.1 owner-admitted schema inventory before routed tool selection.

Tests use controlled in-process native/MCP inventories, never live providers.
"""

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal,
    SessionOwnership,
)
from src.gateway_tool_inventory import (
    AdmittedToolInventory, explicitly_denies_tool_use,
    prepare_admitted_tool_inventory,
)
from src.gateway_turns import GatewayTurns
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.tool_security import delegated_credential_blocked_tools


NATIVE_SCHEMAS = [
    {"type": "function", "function": {"name": "host_shell"}},
    {"type": "function", "function": {"name": "web_search"}},
    {"type": "function", "function": {"name": "manage_notes"}},
    {"type": "function", "function": {"name": "bash"}},
]
MCP_SCHEMAS = [
    {"type": "function", "function": {"name": "mcp__example__read"}},
]


def issued(owner="alice", kind="browser"):
    principal = Principal(
        None if kind == "single_user" else owner,
        kind,
        frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "s1", lambda _: SessionOwnership(owner_id=owner),
    )


def loaded(admission):
    return SimpleNamespace(id=admission.session_id, owner=admission.principal.owner_id)


@pytest.fixture
def inventory_services(monkeypatch):
    from src import agent_loop, tool_schemas, tool_security, tool_utils

    state = {
        "mcp_enabled": True,
        "blocked": frozenset(),
        "fixture_guard": False,
        "owners": [],
        "mcp_disabled_map": {"example": ["deprecated"]},
        "mcp_invocations": [],
        "manager_lookups": [],
    }

    class FakeManager:
        def get_all_openai_schemas(self, disabled_map):
            state["mcp_invocations"].append(disabled_map)
            return list(MCP_SCHEMAS)

    manager = FakeManager()

    def get_manager():
        state["manager_lookups"].append(True)
        return manager if state["mcp_enabled"] else None

    def owner_blocks(owner):
        state["owners"].append(owner)
        return set(state["blocked"])

    monkeypatch.setattr(tool_schemas, "FUNCTION_TOOL_SCHEMAS", NATIVE_SCHEMAS)
    monkeypatch.setattr(tool_utils, "get_mcp_manager", get_manager)
    monkeypatch.setattr(tool_security, "blocked_tools_for_owner", owner_blocks)
    monkeypatch.setattr(
        agent_loop, "_workspace_tools_disabled_for_owner",
        lambda owner: state["fixture_guard"],
    )
    monkeypatch.setattr(
        agent_loop, "_SFT_DISABLED_WORKSPACE_TOOLS",
        frozenset({"bash", "read_file", "write_file"}),
    )
    monkeypatch.setattr(
        agent_loop, "_load_mcp_disabled_map",
        lambda: state["mcp_disabled_map"],
    )
    return state


def run(
    *, admission=None, session=None, context_owner="alice",
    base_policy=None, base_disabled_tools=None, message="Search documentation",
    plan_mode=False, native_workspace_contract=False,
):
    admission = admission if admission is not None else issued()
    session = session if session is not None else loaded(admission)
    return prepare_admitted_tool_inventory(
        admission, session, context_owner=context_owner,
        base_policy=base_policy or ToolPolicy(),
        base_disabled_tools=(
            base_disabled_tools if base_disabled_tools is not None else set()
        ),
        message=message,
        plan_mode=plan_mode,
        native_workspace_contract=native_workspace_contract,
    )


def legacy_inventory_reference(
    *, admission, owner, base_policy, base_disabled_tools, message,
    plan_mode, native_workspace_contract, state,
):
    """The inherited Web ordering, using the isolated fixture's service data."""
    schemas = [s for s in NATIVE_SCHEMAS if s["function"]["name"] != "host_shell"]
    disabled = set(base_disabled_tools)
    blocked = set(state["blocked"])
    if admission.principal.delegated:
        blocked.update(delegated_credential_blocked_tools())
    if state["fixture_guard"] and not native_workspace_contract:
        disabled.update({"bash", "read_file", "write_file"})
    if (state["mcp_enabled"] and not plan_mode
            and not base_policy.disable_mcp and not blocked):
        schemas.extend(MCP_SCHEMAS)
    if explicitly_denies_tool_use(message):
        disabled.update(s["function"]["name"] for s in schemas)
    return (
        tuple(schemas),
        frozenset(blocked),
        frozenset(disabled),
        build_effective_tool_policy(
            disabled_tools=disabled | blocked, last_user_message=message,
        ),
    )


@pytest.mark.parametrize("scenario", [
    {},
    {"mcp_enabled": False},
    {"blocked": {"manage_memory"}},
    {"plan_mode": True},
    {"fixture_guard": True},
    {"fixture_guard": True, "native_workspace_contract": True},
    {"message": "Do not use any tools; explain only."},
    {"base_disabled_tools": {"manage_notes", "web_search"}},
    {"base_policy": build_effective_tool_policy(disabled_tools={"bash"})},
    {"kind": "bearer"},
    {"kind": "single_user"},
])
def test_native_mcp_inventory_matches_legacy_policy_order(
    inventory_services, scenario,
):
    state = inventory_services
    kind = scenario.get("kind", "browser")
    owner = "alice"
    grant = issued(owner, kind)
    state["mcp_enabled"] = scenario.get("mcp_enabled", True)
    state["blocked"] = frozenset(scenario.get("blocked", ()))
    state["fixture_guard"] = scenario.get("fixture_guard", False)
    base_policy = scenario.get("base_policy", ToolPolicy())
    base_disabled = scenario.get(
        "base_disabled_tools", base_policy.all_disabled_names(),
    )
    message = scenario.get("message", "Search documentation")
    plan_mode = scenario.get("plan_mode", False)
    native_workspace = scenario.get("native_workspace_contract", False)

    result = run(
        admission=grant, session=loaded(grant),
        context_owner=grant.principal.owner_id,
        base_policy=base_policy, base_disabled_tools=base_disabled,
        message=message, plan_mode=plan_mode,
        native_workspace_contract=native_workspace,
    )
    reference = legacy_inventory_reference(
        admission=grant, owner=grant.principal.owner_id,
        base_policy=base_policy, base_disabled_tools=base_disabled,
        message=message, plan_mode=plan_mode,
        native_workspace_contract=native_workspace, state=state,
    )
    assert isinstance(result, AdmittedToolInventory)
    assert (
        result.schemas, result.owner_blocked,
        result.disabled_tools, result.policy,
    ) == reference
    assert "host_shell" not in {s["function"]["name"] for s in result.schemas}
    assert state["owners"] == [grant.principal.owner_id]
    assert len(state["manager_lookups"]) == 1
    if "mcp__example__read" in {s["function"]["name"] for s in result.schemas}:
        assert state["mcp_invocations"] == [state["mcp_disabled_map"]]
    else:
        assert state["mcp_invocations"] == []


def test_original_disabled_tools_input_cannot_be_modified(inventory_services):
    disabled = {"web_search"}
    result = run(base_disabled_tools=disabled)
    assert disabled == {"web_search"}
    assert "web_search" in result.disabled_tools


def test_fixture_workspace_guard_is_a_server_veto(inventory_services):
    inventory_services["fixture_guard"] = True
    web = run()
    native = run(native_workspace_contract=True)
    assert {"bash", "read_file", "write_file"} <= web.disabled_tools
    assert "bash" not in native.disabled_tools


def test_delegated_bearer_cannot_reopen_owner_blocked_tools_or_mcp(inventory_services):
    grant = issued(kind="bearer")
    result = run(admission=grant, session=loaded(grant))
    assert delegated_credential_blocked_tools() <= result.owner_blocked
    assert "mcp__example__read" not in {
        s["function"]["name"] for s in result.schemas
    }
    assert result.owner_blocked <= result.policy.all_disabled_names()


@pytest.mark.parametrize("message,expected", [
    ("No tools, please.", True),
    ("Use no tool.", True),
    ("do not use any tools", True),
    ("Don't use tools", True),
    ("Search the web", False),
    ("No toolbars in this UI", False),
    ("Answer from memory only", False),
])
def test_explicit_no_tools_classifier_retains_inherited_wording(message, expected):
    from routes.chat_routes import _explicitly_denies_tool_use
    assert explicitly_denies_tool_use(message) is expected
    assert _explicitly_denies_tool_use(message) is expected


def test_guide_only_denies_every_schema_without_mcp_expansion(inventory_services):
    policy = build_effective_tool_policy(
        last_user_message="Do not use any tools.",
    )
    result = run(
        message="Do not use any tools.", base_policy=policy,
        base_disabled_tools=policy.all_disabled_names(),
    )
    names = {s["function"]["name"] for s in result.schemas}
    assert names <= result.disabled_tools
    assert "mcp__example__read" not in names
    assert result.policy.block_all_tool_calls
    assert result.policy.disable_mcp


def test_non_monotonic_base_guide_only_policy_fails_closed(inventory_services):
    policy = build_effective_tool_policy(
        last_user_message="Do not use any tools.",
    )
    with pytest.raises(AdmissionDenied) as error:
        run(
            base_policy=policy, base_disabled_tools=set(),
            message="Please use the tools",
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id",
    "principal", "loaded_session", "loaded_owner", "context_owner",
])
def test_bad_admission_never_reads_inventory_or_mcp(
    inventory_services, tamper,
):
    grant = issued()
    session = loaded(grant)
    owner = "alice"
    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "s1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session_id":
        grant = replace(grant, session_id="foreign")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded_session":
        session.id = "foreign"
    elif tamper == "loaded_owner":
        session.owner = "bob"
    elif tamper == "context_owner":
        owner = "bob"
    with pytest.raises(AdmissionDenied):
        prepare_admitted_tool_inventory(
            grant, session, context_owner=owner,
            base_policy=ToolPolicy(), base_disabled_tools=set(),
            message="Search", plan_mode=False,
            native_workspace_contract=False,
        )
    assert inventory_services["owners"] == []
    assert inventory_services["manager_lookups"] == []
    assert inventory_services["mcp_invocations"] == []


@pytest.mark.parametrize("overrides", [
    {"base_policy": None},
    {"base_policy": object()},
    {"base_disabled_tools": ["bash"]},
    {"plan_mode": "false"},
    {"native_workspace_contract": 1},
    {"message": object()},
])
def test_invalid_trusted_policy_and_flags_fail_before_inventory(
    inventory_services, overrides,
):
    grant = issued()
    args = dict(
        context_owner="alice", base_policy=ToolPolicy(),
        base_disabled_tools=set(), message="Search",
        plan_mode=False, native_workspace_contract=False,
    )
    args.update(overrides)
    with pytest.raises(AdmissionDenied):
        prepare_admitted_tool_inventory(grant, loaded(grant), **args)
    assert inventory_services["owners"] == []
    assert inventory_services["manager_lookups"] == []


def test_gateway_turns_facade_reuses_same_inventory_service(inventory_services):
    grant = issued()
    args = dict(
        context_owner="alice", base_policy=ToolPolicy(),
        base_disabled_tools={"web_search"}, message="List notes",
        plan_mode=False, native_workspace_contract=False,
    )
    facade = GatewayTurns(MagicMock()).prepare_tool_inventory(
        grant, loaded(grant), **args,
    )
    direct = prepare_admitted_tool_inventory(grant, loaded(grant), **args)
    assert facade == direct


def test_service_never_imports_web_transport():
    from src import gateway_tool_inventory

    tree = ast.parse(inspect.getsource(gateway_tool_inventory))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        elif isinstance(node, ast.Import):
            assert all(
                not entry.name.startswith(("routes", "fastapi", "starlette"))
                for entry in node.names
            )


def test_web_route_prepares_schema_inventory_only_through_gateway():
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
        and node.func.attr == "prepare_tool_inventory"
    ]
    assert len(calls) == 1
    named = {kw.arg: kw.value for kw in calls[0].keywords}
    assert isinstance(named["context_owner"], ast.Attribute)
    assert named["context_owner"].attr == "user"
    assert isinstance(named["base_policy"], ast.Name)
    assert named["base_policy"].id == "tool_policy"
    assert isinstance(named["base_disabled_tools"], ast.Name)
    assert named["base_disabled_tools"].id == "disabled_tools"
    assert source.index("gateway_turns.prepare_tool_inventory(") < source.index(
        "gateway_turns.resolve_routed_contract("
    )
    assert "_contract_mgr = get_mcp_manager()" not in source
    assert "_owner_blocked = blocked_tools_for_owner(_user)" not in source
