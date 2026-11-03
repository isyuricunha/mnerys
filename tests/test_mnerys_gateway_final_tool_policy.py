"""M2.1: final account, global and mode tool policy belongs to the Gateway.

These are in-process principal projections, not a remote Gateway/API test.
No real user configuration, model endpoint or database is used here.
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
from src.gateway_execution_policy import (
    FinalTurnToolPolicy, finalize_admitted_turn_tool_policy,
)
from src.gateway_turns import GatewayTurns
from src.tool_security import delegated_credential_blocked_tools, plan_mode_disabled_tools
from src.turn_contract import FAMILY_TOOLS


BROWSER_TOOLS = frozenset({"mcp__browser__open", "mcp__browser__click"})
_DEFAULT_ADMISSION = object()


def issued(kind="browser", *, owner="alice", session_id="example-session"):
    principal = Principal(
        owner, kind, frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session_id, lambda _: SessionOwnership(owner_id=owner),
    )


def decide(
    *,
    admission=_DEFAULT_ADMISSION,
    session=None,
    context_owner="alice",
    base_disabled_tools=None,
    privileges=None,
    global_disabled_tools=None,
    browser_tools=BROWSER_TOOLS,
    last_user_message="Inspect this.",
    chat_mode="agent",
    auto_escalated=False,
    workspace_agent_intent=False,
    use_turn_contract=False,
    allow_browser_for_web_turn=False,
    compare_mode=False,
    plan_mode=False,
):
    admission = issued() if admission is _DEFAULT_ADMISSION else admission
    session = (
        SimpleNamespace(id=admission.session_id, owner=admission.principal.owner_id, name="Chat")
        if session is None else session
    )
    return finalize_admitted_turn_tool_policy(
        admission, session, context_owner=context_owner,
        base_disabled_tools=base_disabled_tools if base_disabled_tools is not None else set(),
        privileges=privileges,
        global_disabled_tools=global_disabled_tools,
        browser_tools=browser_tools,
        last_user_message=last_user_message,
        chat_mode=chat_mode,
        auto_escalated=auto_escalated,
        workspace_agent_intent=workspace_agent_intent,
        use_turn_contract=use_turn_contract,
        allow_browser_for_web_turn=allow_browser_for_web_turn,
        compare_mode=compare_mode,
        plan_mode=plan_mode,
    )


def test_unrestricted_browser_keeps_inherited_default_agent_policy():
    result = decide()
    assert isinstance(result, FinalTurnToolPolicy)
    assert result.allow_agent is True
    assert result.allow_research is True
    assert result.chat_mode == "agent"
    assert result.policy.all_disabled_names() == set()


def test_denylist_is_copied_and_intent_cannot_restore_global_denials():
    base = {"manage_calendar", "web_search"}
    result = decide(
        base_disabled_tools=base,
        global_disabled_tools=["bash", "manage_memory", "web_fetch"],
        last_user_message="Now use web search to complete this.",
    )
    assert base == {"manage_calendar", "web_search"}
    assert result.policy.all_disabled_names() >= {
        "manage_calendar", "web_search", "bash", "manage_memory", "web_fetch"
    }
    assert not result.policy.blocks("manage_tasks")


@pytest.mark.parametrize("kind", ["browser", "bearer"])
def test_account_denials_are_applied_after_intent_for_every_credential(kind):
    result = decide(
        admission=issued(kind),
        privileges={
            "can_use_bash": False, "can_use_browser": False,
            "can_generate_images": False, "can_manage_memory": False,
            "can_use_research": False, "can_use_agent": False,
        },
    )
    disabled = result.policy.all_disabled_names()
    assert disabled >= set(FAMILY_TOOLS["shell_files"])
    assert disabled >= set(BROWSER_TOOLS)
    assert disabled >= {
        "private_browser", "edit_image", "generate_image",
        "manage_memory", "manage_skills",
        "manage_research", "trigger_research",
    }
    assert result.allow_agent is False
    assert result.allow_research is False
    assert result.chat_mode == "chat"
    if kind == "bearer":
        assert disabled >= delegated_credential_blocked_tools()


def test_bearer_admin_cannot_override_delegated_restrictions():
    privileges = {flag: True for flag in (
        "can_use_bash", "can_use_browser", "can_use_documents",
        "can_generate_images", "can_manage_memory", "can_use_research", "can_use_agent",
    )}
    result = decide(admission=issued("bearer"), privileges=privileges)
    assert result.policy.all_disabled_names() >= delegated_credential_blocked_tools()


@pytest.mark.parametrize("workspace,contract,allow_browser,expected_browser", [
    (False, False, False, True),
    (False, False, True, False),
    (True, False, False, False),
    (False, True, False, False),
])
def test_light_auto_escalation_clamp_only_applies_to_personal_turn(
    workspace, contract, allow_browser, expected_browser,
):
    result = decide(
        auto_escalated=True,
        workspace_agent_intent=workspace,
        use_turn_contract=contract,
        allow_browser_for_web_turn=allow_browser,
    )
    disabled = result.policy.all_disabled_names()
    assert ("bash" in disabled) == (not workspace and not contract)
    assert ("mcp__browser__open" in disabled) is expected_browser


def test_compare_chat_clamps_tools_but_agent_retains_read_capabilities():
    for mode in ("chat", "agent"):
        result = decide(compare_mode=True, chat_mode=mode)
        disabled = result.policy.all_disabled_names()
        assert disabled >= {"create_document", "edit_document", "manage_memory", "ui_control"}
        assert ("bash" in disabled) == (mode == "chat")
        assert ("web_search" in disabled) == (mode == "chat")



@pytest.mark.parametrize("legacy_mode", ["", "unknown"])
def test_legacy_omitted_mode_stays_unchanged_but_compare_fails_closed(legacy_mode):
    normal = decide(chat_mode=legacy_mode)
    assert normal.chat_mode == legacy_mode
    assert "bash" not in normal.policy.all_disabled_names()
    compared = decide(chat_mode=legacy_mode, compare_mode=True)
    assert compared.chat_mode == legacy_mode
    assert {"bash", "web_fetch", "manage_tasks"} <= compared.policy.all_disabled_names()


def test_agent_privilege_demotion_precedes_compare_chat_clamp():
    result = decide(compare_mode=True, privileges={"can_use_agent": False})
    assert result.chat_mode == "chat"
    assert {"bash", "web_fetch", "manage_tasks"} <= result.policy.all_disabled_names()


def test_compare_session_label_always_denies_document_edits():
    session = SimpleNamespace(id="example-session", owner="alice", name="[CMP] selected")
    result = decide(session=session)
    assert {"create_document", "edit_document", "update_document"} <= result.policy.all_disabled_names()
    assert "manage_memory" not in result.policy.all_disabled_names()


def test_plan_mode_keeps_read_only_tool_guard_and_global_denials():
    result = decide(plan_mode=True, global_disabled_tools=["read_file"])
    assert result.policy.all_disabled_names() >= plan_mode_disabled_tools()
    assert result.policy.blocks("read_file")
    assert result.policy.blocks("send_email")


def test_explicit_guide_only_user_turn_still_blocks_every_tool():
    result = decide(last_user_message="Just explain the steps, do not use any tools.")
    assert result.policy.block_all_tool_calls is True
    assert result.policy.disable_mcp is True


def test_irrelevant_global_setting_shapes_do_not_change_original_policy():
    assert decide(global_disabled_tools=None).policy.all_disabled_names() == set()
    assert decide(global_disabled_tools="bash").policy.all_disabled_names() == set()
    assert decide(global_disabled_tools={"bash"}).policy.all_disabled_names() == set()


@pytest.mark.parametrize("tamper", ["none", "forged", "proof", "session", "principal", "loaded", "context"])
def test_invalid_admission_or_owner_denies_before_policy_composition(tamper):
    grant = issued()
    context_owner = "alice"
    sess = SimpleNamespace(id="example-session", owner="alice", name="Chat")
    if tamper == "none":
        grant = None
    elif tamper == "forged":
        grant = AdmittedSession(Principal("alice", "browser"), "example-session")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session":
        grant = replace(grant, session_id="foreign")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded":
        sess.owner = "bob"
    elif tamper == "context":
        context_owner = "bob"

    with pytest.raises(AdmissionDenied) as raised:
        decide(admission=grant, session=sess, context_owner=context_owner)
    assert raised.value.status_code == 403


@pytest.mark.parametrize("overrides", [
    {"privileges": []},
    {"base_disabled_tools": ["bash"]},
    {"browser_tools": {"private_browser"}},
    {"chat_mode": None},
    {"auto_escalated": 1},
    {"compare_mode": "false"},
    {"plan_mode": "true"},
])
def test_invalid_trusted_configuration_fails_closed(overrides):
    with pytest.raises(AdmissionDenied):
        decide(**overrides)


def test_gateway_facade_exposes_same_admitted_final_policy():
    grant = issued()
    args = dict(
        context_owner="alice",
        base_disabled_tools={"manage_memory"},
        privileges={"can_use_agent": False},
        global_disabled_tools=["web_search"],
        browser_tools=BROWSER_TOOLS,
        last_user_message="Hello",
        chat_mode="agent",
        auto_escalated=False,
        workspace_agent_intent=False,
        use_turn_contract=False,
        allow_browser_for_web_turn=False,
        compare_mode=True,
        plan_mode=False,
    )
    session = SimpleNamespace(id=grant.session_id, owner="alice", name="Chat")
    gateway = GatewayTurns(MagicMock())
    result = gateway.finalize_tool_policy(grant, session, **args)
    assert result == finalize_admitted_turn_tool_policy(grant, session, **args)


def test_src_finalizer_does_not_import_web_routes_or_http():
    from src import gateway_execution_policy

    tree = ast.parse(inspect.getsource(gateway_execution_policy))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        if isinstance(node, ast.Import):
            assert all(not name.name.startswith(("routes", "fastapi", "starlette")) for name in node.names)





@pytest.mark.asyncio
async def test_stream_adapter_preserves_legacy_omitted_chat_mode(monkeypatch):
    """A minimal API FormData payload still reaches the existing Chat path."""
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    request = _RouteRequest("chat")
    request._form.pop("mode")
    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass
    # The legacy adapter can auto-route by intent; omission must not 403.
    assert "chat" in captured or "agent" in captured


def test_web_handoff_calls_gateway_once_after_request_intent_routing():
    source = (Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py").read_text()
    tree = ast.parse(source)
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "finalize_tool_policy"
    ]
    assert len(matches) == 1
    kwargs = {kw.arg: kw.value for kw in matches[0].keywords}
    assert isinstance(kwargs["context_owner"], ast.Attribute)
    assert kwargs["context_owner"].attr == "user"
    assert isinstance(kwargs["privileges"], ast.Name)
    assert kwargs["privileges"].id == "_privs"
    assert isinstance(kwargs["browser_tools"], ast.Call)
    assert source.index("disabled_tools.difference_update(WEB_TOOL_NAMES)") < source.index("gateway_turns.finalize_tool_policy(")
