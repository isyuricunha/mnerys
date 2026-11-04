"""M2.1 admitted post-capability Web intent and denylist parity.

The independent reference recreates the inherited Web adapter's ordering.
No real server, external search, MCP service or production DB is used.
"""

import ast
import inspect
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_turns import GatewayTurns
from src.gateway_web_intent import AdmittedWebIntent, reconcile_admitted_web_intent
from src.tool_policy import (
    WEB_TOOL_NAMES, is_web_search_explicitly_denied,
    web_intent_may_enable_for_turn,
)


def admitted(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential, frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "s1", lambda _: SessionOwnership(owner_id=owner),
    )


def context(**overrides):
    owner = overrides.pop("owner", "alice")
    credential = overrides.pop("credential", "browser")
    grant = admitted(owner, credential)
    session = SimpleNamespace(id="s1", owner=grant.principal.owner_id)
    options = {
        "context_owner": grant.principal.owner_id,
        "message": "Search public information",
        "explicit_web_intent": False,
        "personal_store_intent": False,
        "tool_intent_is_web": False,
        "capabilities": frozenset({"search_browser"}),
        "clean_route_requested": False,
        "allow_web_search": None,
        "search_enabled": False,
        "explicit_browser_intent": False,
        "local_browser_render_intent": False,
        "exact_url_fetch_intent": False,
        "use_turn_contract": True,
        "full_schema_route": False,
        "workspace_bound": False,
        "base_disabled_tools": set(),
        "web_link_followup_lookup": MagicMock(return_value=False),
        "contextual_web_followup_lookup": MagicMock(return_value=False),
        "recent_web_event_lookup": MagicMock(return_value=False),
        "message_denies_lookup": MagicMock(return_value=False),
    }
    options.update(overrides)
    return grant, session, options


def original_web_reference(**p):
    """Independent, test-only copy of the old route's post-capability branch."""
    message = p["message"]
    disabled = set(p["base_disabled_tools"])
    explicit = p["explicit_web_intent"]
    tool_is_web = p["tool_intent_is_web"]
    personal = p["personal_store_intent"]
    past_chat = bool(
        isinstance(message, str)
        and re.search(r"\b(?:search|find|look\s*up)\b", message, re.IGNORECASE)
        and re.search(
            r"\b(?:prior|past|previous|old)\s+(?:chats?|sessions?|conversations?)\b",
            message, re.IGNORECASE,
        )
    )
    if past_chat:
        explicit = False
    explicit = explicit or bool(
        tool_is_web and not personal and not past_chat
    )
    link = p["web_link_followup_lookup"]()
    followup = bool(
        "search_browser" in p["capabilities"]
        and p["contextual_web_followup_lookup"]()
        and p["recent_web_event_lookup"]()
        and not p["message_denies_lookup"]()
    )
    clean = bool(
        p["clean_route_requested"]
        and "search_browser" in p["capabilities"]
        and not p["message_denies_lookup"]()
    )
    enabled = p["search_enabled"]
    toggle = p["allow_web_search"]
    if (
        (explicit or link or followup or clean)
        and web_intent_may_enable_for_turn(
            None if (followup or clean) else toggle,
            message_denies_lookup=p["message_denies_lookup"](),
        )
    ):
        enabled = True
        toggle = "true"
    if is_web_search_explicitly_denied(toggle) or not enabled:
        disabled.update(WEB_TOOL_NAMES)
        if not p["explicit_browser_intent"]:
            disabled.add("youtube_tool")
        if not (p["explicit_browser_intent"] or p["local_browser_render_intent"]):
            disabled.add("private_browser")
    if p["exact_url_fetch_intent"]:
        disabled.discard("web_fetch")
    if explicit and not p["use_turn_contract"] and not p["full_schema_route"]:
        disabled.update({
            "search_chats", "manage_skills", "manage_memory",
            "create_document", "edit_document", "update_document",
            "send_email", "reply_to_email",
            "manage_notes", "manage_calendar", "manage_tasks", "api_call",
        })
        web_workspace_output = bool(
            p["workspace_bound"]
            and isinstance(message, str)
            and re.search(r"(?:^|\s)/workspace/[^\s]+", message)
            and re.search(
                r"(?:\b(?:create|generate|save|write|render|export|produce|build|make)\b|"
                r"创建|生成|保存|写入|制作|截取|剪辑|拼接|导出)",
                message, re.IGNORECASE,
            )
        )
        if not web_workspace_output:
            disabled.update({"bash", "python", "read_file", "write_file", "edit_file"})
        if enabled:
            disabled.difference_update(WEB_TOOL_NAMES)
        else:
            disabled.update(WEB_TOOL_NAMES)
    elif enabled:
        disabled.difference_update(WEB_TOOL_NAMES)
    return (
        explicit, followup, enabled, toggle, frozenset(disabled),
    )


@pytest.mark.parametrize("case", [
    {},
    {"explicit_web_intent": True},
    {"tool_intent_is_web": True},
    {"explicit_web_intent": True, "personal_store_intent": True},
    {"tool_intent_is_web": True, "personal_store_intent": True},
    {"explicit_web_intent": True, "message": "Search my past chats"},
    {"explicit_web_intent": True, "message": "Look up previous sessions"},
    {
        "message": "Search my old chats", "tool_intent_is_web": True,
        "explicit_web_intent": True,
    },
    {"explicit_web_intent": True, "allow_web_search": "false"},
    {"tool_intent_is_web": True, "allow_web_search": "false"},
    {"explicit_web_intent": True, "allow_web_search": True},
    {"explicit_web_intent": True, "search_enabled": True},
    {"explicit_web_intent": True, "allow_web_search": "false", "search_enabled": True},
    {"exact_url_fetch_intent": True},
    {"exact_url_fetch_intent": True, "allow_web_search": "false"},
    {
        "explicit_browser_intent": True, "allow_web_search": "false",
    },
    {"local_browser_render_intent": True},
    {"web_link_followup_lookup": MagicMock(return_value=True)},
    {"web_link_followup_lookup": MagicMock(return_value=True),
     "allow_web_search": "false"},
    {"contextual_web_followup_lookup": MagicMock(return_value=True),
     "recent_web_event_lookup": MagicMock(return_value=True)},
    {
        "contextual_web_followup_lookup": MagicMock(return_value=True),
        "recent_web_event_lookup": MagicMock(return_value=False),
    },
    {
        "contextual_web_followup_lookup": MagicMock(return_value=True),
        "recent_web_event_lookup": MagicMock(return_value=True),
        "allow_web_search": "false",
    },
    {"clean_route_requested": True},
    {"clean_route_requested": True, "allow_web_search": "false"},
    {"clean_route_requested": True, "capabilities": frozenset({"notes"})},
    {"explicit_web_intent": True, "message_denies_lookup": MagicMock(return_value=True)},
    {"clean_route_requested": True, "message_denies_lookup": MagicMock(return_value=True)},
    {"explicit_web_intent": True, "use_turn_contract": False},
    {"explicit_web_intent": True, "use_turn_contract": False, "full_schema_route": True},
    {
        "explicit_web_intent": True, "use_turn_contract": False,
        "workspace_bound": True, "message": "Create /workspace/report.md",
    },
    {
        "explicit_web_intent": True, "use_turn_contract": False,
        "workspace_bound": False, "message": "Create /workspace/report.md",
    },
    {"search_enabled": True, "base_disabled_tools": {"web_fetch", "bash"}},
    {"search_enabled": False, "base_disabled_tools": {"web_fetch", "bash"}},
    {"tool_intent_is_web": True, "capabilities": frozenset({"calendar"})},
])
def test_web_intent_matches_inherited_route_and_lazy_evidence(case):
    grant, sess, params = context(**case)
    actual = reconcile_admitted_web_intent(grant, sess, **params)
    expected_kwargs = dict(params)
    copied = {
        name: MagicMock(return_value=fn.return_value)
        for name in (
            "web_link_followup_lookup",
            "contextual_web_followup_lookup",
            "recent_web_event_lookup",
            "message_denies_lookup",
        )
        if (fn := params[name])
    }
    expected_kwargs.update(copied)
    expected = original_web_reference(**expected_kwargs)
    assert isinstance(actual, AdmittedWebIntent)
    assert (
        actual.explicit_web_intent,
        actual.contextual_web_turn_followup,
        actual.search_enabled,
        actual.allow_web_search,
        actual.disabled_tools,
    ) == expected
    for name, mock in copied.items():
        assert params[name].call_count == mock.call_count, name


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
def test_credential_projections_share_same_gateway_service(credential):
    grant, sess, params = context(
        credential=credential, explicit_web_intent=True,
        search_enabled=False,
    )
    direct = reconcile_admitted_web_intent(grant, sess, **params)
    facade = GatewayTurns(MagicMock()).reconcile_web_intent(grant, sess, **params)
    assert facade == direct


def test_past_chat_lookup_never_enables_public_search():
    grant, sess, params = context(
        message="Search my past chats", explicit_web_intent=True,
        capabilities=frozenset({"sessions"}),
    )
    result = reconcile_admitted_web_intent(grant, sess, **params)
    assert not result.explicit_web_intent
    assert not result.search_enabled
    assert "web_search" in result.disabled_tools


def test_referential_web_requires_actual_prior_success_and_no_denial():
    grant, sess, params = context(
        contextual_web_followup_lookup=MagicMock(return_value=True),
        recent_web_event_lookup=MagicMock(return_value=False),
        allow_web_search="false",
    )
    result = reconcile_admitted_web_intent(grant, sess, **params)
    assert not result.contextual_web_turn_followup
    assert "web_search" in result.disabled_tools
    params["recent_web_event_lookup"].return_value = True
    result = reconcile_admitted_web_intent(grant, sess, **params)
    assert result.contextual_web_turn_followup
    assert result.search_enabled
    # This is inherited fallback behavior. The final privilege/policy veto
    # still restricts tools independently of a model-facing schema.
    assert result.allow_web_search == "true"


def test_explicit_denial_vetoes_inferred_search_without_forced_enable():
    grant, sess, params = context(
        explicit_web_intent=True, message_denies_lookup=MagicMock(return_value=True),
    )
    result = reconcile_admitted_web_intent(grant, sess, **params)
    assert not result.search_enabled
    assert "web_search" in result.disabled_tools


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
])
def test_invalid_session_denied_before_history_or_policy(tamper):
    grant, sess, params = context()
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
        sess.id = "other"
    elif tamper == "loaded_owner":
        sess.owner = "bob"
    else:
        params["context_owner"] = "bob"
    with pytest.raises(AdmissionDenied) as raised:
        reconcile_admitted_web_intent(grant, sess, **params)
    assert raised.value.status_code == 403
    for key in ("web_link_followup_lookup", "contextual_web_followup_lookup",
                "recent_web_event_lookup", "message_denies_lookup"):
        params[key].assert_not_called()


@pytest.mark.parametrize("changed", [
    {"message": None},
    {"explicit_web_intent": 1},
    {"personal_store_intent": "false"},
    {"tool_intent_is_web": None},
    {"capabilities": {"search_browser"}},
    {"capabilities": frozenset({1})},
    {"clean_route_requested": 1},
    {"search_enabled": "true"},
    {"explicit_browser_intent": None},
    {"local_browser_render_intent": "yes"},
    {"exact_url_fetch_intent": 1},
    {"use_turn_contract": None},
    {"full_schema_route": "false"},
    {"workspace_bound": 1},
    {"base_disabled_tools": ("bash",)},
    {"web_link_followup_lookup": None},
    {"contextual_web_followup_lookup": False},
    {"recent_web_event_lookup": "true"},
    {"message_denies_lookup": None},
])
def test_untrusted_types_fail_closed_before_any_port_call(changed):
    grant, sess, params = context()
    params.update(changed)
    with pytest.raises(AdmissionDenied):
        reconcile_admitted_web_intent(grant, sess, **params)
    for key in ("web_link_followup_lookup", "contextual_web_followup_lookup",
                "recent_web_event_lookup", "message_denies_lookup"):
        if isinstance(params[key], MagicMock):
            params[key].assert_not_called()


@pytest.mark.parametrize(("port", "options"), [
    ("web_link_followup_lookup", {}),
    ("contextual_web_followup_lookup", {}),
    ("recent_web_event_lookup", {"contextual_web_followup_lookup": MagicMock(return_value=True)}),
    ("message_denies_lookup", {"explicit_web_intent": True}),
])
def test_invalid_runtime_evidence_is_rejected(port, options):
    grant, sess, params = context(**options)
    params[port] = MagicMock(return_value="true")
    with pytest.raises(AdmissionDenied):
        reconcile_admitted_web_intent(grant, sess, **params)


def test_source_adaptor_delegates_before_incognito_and_final_account_veto():
    source=(Path(__file__).resolve().parents[1]/"routes/chat_routes.py").read_text()
    tree=ast.parse(source)
    calls=[
        node for node in ast.walk(tree)
        if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Attribute)
        and isinstance(node.func.value,ast.Name)
        and node.func.value.id=="gateway_turns"
        and node.func.attr=="reconcile_web_intent"
    ]
    assert len(calls)==1
    kwargs={kv.arg:kv.value for kv in calls[0].keywords}
    assert isinstance(kwargs["context_owner"],ast.Attribute)
    assert kwargs["context_owner"].attr=="user"
    assert source.index("gateway_turns.classify_turn_capabilities(") < source.index(
        "gateway_turns.reconcile_web_intent("
    ) < source.index("gateway_turns.finalize_tool_policy(")
    assert "_explicit_past_chat_search_intent = bool(" not in source


def test_gateway_service_has_no_route_or_transport_imports():
    from src import gateway_web_intent
    tree=ast.parse(inspect.getsource(gateway_web_intent))
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            assert not (node.module or "").startswith(("routes","fastapi","starlette"))
        elif isinstance(node,ast.Import):
            assert all(
                not entry.name.startswith(("routes","fastapi","starlette"))
                for entry in node.names
            )
