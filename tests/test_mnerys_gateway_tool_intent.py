"""M2.1 admitted exact tool selection and typed context follow-up parity."""

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
from src.gateway_tool_inventory import AdmittedToolInventory
from src.gateway_tool_intent import AdmittedToolIntent, select_admitted_tool_intent
from src.gateway_turns import GatewayTurns
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.turn_contract import (
    preserve_bound_editor_selected_tools, requests_independent_web_source,
)


def issued(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "s1", lambda _sid: SessionOwnership(owner_id=owner),
    )


def loaded(admission):
    return SimpleNamespace(
        id=admission.session_id, owner=admission.principal.owner_id,
    )


def inventory(*, disabled=(), blocked=(), message="List my notes"):
    denied = set(disabled)
    owner_blocked = frozenset(blocked)
    return AdmittedToolInventory(
        schemas=(),
        owner_blocked=owner_blocked,
        disabled_tools=frozenset(denied),
        policy=build_effective_tool_policy(
            disabled_tools=denied | owner_blocked, last_user_message=message,
        ),
    )


def args(*, selected=None, owner="alice", credential="browser", message="List my notes",
         disabled=(), blocked=(), capabilities=("notes",), active=None,
         recent_web_tool=None, active_document=False,
         contextual_browser_followup=False, contextual_web_followup=False,
         active_email_review=False, corrected_browser_target=False,
         warm_tools=frozenset()):
    grant = issued(owner, credential)
    sess = loaded(grant)
    p = dict(
        context_owner=grant.principal.owner_id,
        inventory=inventory(disabled=disabled, blocked=blocked, message=message),
        message=message,
        capabilities=frozenset(capabilities),
        active_capabilities=frozenset(capabilities) if active is None else frozenset(active),
        warm_tools=warm_tools,
        recent_web_tool_lookup=MagicMock(return_value=recent_web_tool),
        active_document=active_document,
        contextual_browser_followup=contextual_browser_followup,
        contextual_web_followup=contextual_web_followup,
        active_email_review=active_email_review,
        corrected_browser_target=corrected_browser_target,
    )
    return grant, sess, p


def original_web_reference(*, selected, owner, p):
    """Independent test-only transcription of the original route order."""
    chosen = selected
    if chosen is None and p["contextual_browser_followup"]:
        chosen = frozenset({"private_browser"})
    required = set(chosen or ())
    chosen = preserve_bound_editor_selected_tools(
        p["message"], chosen, active_document=p["active_document"],
    )
    disabled = set(p["inventory"].disabled_tools)
    blocked = set(p["inventory"].owner_blocked)
    fixture_personal = (
        set(chosen or ()) & {"manage_notes", "manage_calendar", "manage_tasks"}
    ) - disabled - blocked
    caps = p["capabilities"]
    active = p["active_capabilities"]
    policy = p["inventory"].policy
    if str(owner or "").startswith("sft_") and fixture_personal:
        names = {
            "manage_notes": "notes",
            "manage_calendar": "calendar",
            "manage_tasks": "tasks",
        }
        caps = frozenset(set(caps) | {names[n] for n in fixture_personal})
        active = caps
        policy = build_effective_tool_policy(
            disabled_tools=disabled | blocked, last_user_message=p["message"],
        )
    recent = p["recent_web_tool_lookup"].return_value
    if (chosen == {"web_search"} and requests_independent_web_source(p["message"])
            and recent in {"web_search", "web_fetch"}):
        chosen = {"web_fetch"}
    if chosen is None and p["contextual_web_followup"] and recent:
        chosen = {recent}
    if chosen is None and p["active_email_review"] and "email" in caps:
        caps = caps | {"ui"}
        required.add("ui_control")
    if p["corrected_browser_target"]:
        chosen = {"private_browser", "web_fetch", "web_search"}
        required = {"private_browser"}
    return (
        None if chosen is None else frozenset(chosen),
        frozenset(required),
        frozenset(caps),
        frozenset(active),
        policy,
        frozenset(fixture_personal),
    )


@pytest.mark.parametrize("case", [
    {"selected": None},
    {"selected": set()},
    {"selected": {"web_search"}},
    {"selected": None, "contextual_browser_followup": True},
    {"selected": None, "contextual_web_followup": True, "recent_web_tool": "web_fetch"},
    {"selected": None, "contextual_web_followup": True, "recent_web_tool": "get_weather"},
    {"selected": None, "contextual_web_followup": True, "recent_web_tool": None},
    {"selected": {"web_search"}, "recent_web_tool": "web_search",
     "message": "Double-check with another reliable source link"},
    {"selected": {"web_search"}, "recent_web_tool": None,
     "message": "Double-check with another reliable source link"},
    {"selected": {"web_search"}, "recent_web_tool": "web_search",
     "message": "Double-check with another reliable source link",
     "corrected_browser_target": True},
    {"selected": {"manage_calendar"}, "owner": "sft_example",
     "capabilities": ("email",), "disabled": ()},
    {"selected": {"manage_calendar"}, "owner": "sft_example",
     "capabilities": ("email",), "disabled": ("manage_calendar",)},
    {"selected": {"manage_notes"}, "owner": "sft_example",
     "capabilities": ("calendar",), "blocked": ("manage_notes",)},
    {"selected": {"manage_tasks"}, "owner": "alice", "capabilities": ("email",)},
    {"selected": None, "capabilities": ("email",), "active_email_review": True},
    {"selected": None, "capabilities": ("email",), "active_email_review": False},
    {"selected": {"web_search"}, "capabilities": ("email",), "active_email_review": True},
    {"selected": {"create_document"}, "active_document": True,
     "message": "Please rewrite the open document for clarity"},
    {"selected": {"inspect_media", "write_file"}, "warm_tools": frozenset({"read_file"})},
    {"selected": None, "corrected_browser_target": True,
     "contextual_browser_followup": True, "contextual_web_followup": True,
     "recent_web_tool": "web_fetch"},
])
def test_exact_selection_matches_previous_web_order(monkeypatch, case):
    from src import gateway_tool_intent as service

    selected = case["selected"]
    monkeypatch.setattr(
        service, "selected_tools_for_request",
        MagicMock(return_value=selected),
    )
    grant, sess, p = args(**case)
    expected = original_web_reference(
        selected=selected, owner=grant.principal.owner_id, p=p,
    )
    result = select_admitted_tool_intent(grant, sess, **p)
    assert isinstance(result, AdmittedToolIntent)
    assert (
        result.selected_tools, result.required_tools,
        result.capabilities, result.active_capabilities, result.policy,
        result.fixture_personal_tools,
    ) == expected
    assert result.warm_tools is p["warm_tools"]
    expected_lookup = bool(
        (selected is None and not p["contextual_browser_followup"]
         and p["contextual_web_followup"])
        or (
            selected == {"web_search"}
            and requests_independent_web_source(p["message"])
        )
    )
    assert p["recent_web_tool_lookup"].call_count == int(expected_lookup)


@pytest.mark.parametrize("kind", ["browser", "bearer", "single_user"])
def test_same_gateway_service_for_all_admitted_credentials(kind, monkeypatch):
    from src import gateway_tool_intent as service
    monkeypatch.setattr(
        service, "selected_tools_for_request",
        lambda text: frozenset({"web_search"}),
    )
    grant, sess, p = args(credential=kind, capabilities=("search_browser",))
    direct = select_admitted_tool_intent(grant, sess, **p)
    facade = GatewayTurns(MagicMock()).select_tool_intent(grant, sess, **p)
    assert direct == facade
    assert direct.required_tools == frozenset({"web_search"})


def test_none_selection_preserves_family_routing_and_empty_selection_does_not(monkeypatch):
    from src import gateway_tool_intent as service
    grant, sess, p = args(capabilities=("calendar", "notes"))
    monkeypatch.setattr(service, "selected_tools_for_request", lambda _: None)
    fallback = select_admitted_tool_intent(grant, sess, **p)
    assert fallback.selected_tools is None
    assert fallback.required_tools == frozenset()
    assert fallback.capabilities == frozenset({"calendar", "notes"})
    monkeypatch.setattr(service, "selected_tools_for_request", lambda _: frozenset())
    empty = select_admitted_tool_intent(grant, sess, **p)
    assert empty.selected_tools == frozenset()


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
])
def test_denied_admission_never_invokes_classifier_or_history(monkeypatch, tamper):
    from src import gateway_tool_intent as service
    classify = MagicMock(side_effect=AssertionError("classifier called"))
    monkeypatch.setattr(service, "selected_tools_for_request", classify)
    grant, sess, p = args(contextual_web_followup=True)
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
    elif tamper == "context_owner":
        p["context_owner"] = "bob"
    with pytest.raises(AdmissionDenied) as raised:
        select_admitted_tool_intent(grant, sess, **p)
    assert raised.value.status_code == 403
    classify.assert_not_called()
    p["recent_web_tool_lookup"].assert_not_called()


@pytest.mark.parametrize("override", [
    {"inventory": object()},
    {"message": None},
    {"warm_tools": {"web_fetch"}},
    {"warm_tools": frozenset({1})},
    {"recent_web_tool_lookup": "web_search"},
    {"capabilities": None},
    {"active_capabilities": ("notes",)},
    {"active_document": "true"},
    {"contextual_browser_followup": 1},
    {"contextual_web_followup": None},
    {"active_email_review": "false"},
    {"corrected_browser_target": []},
])
def test_malformed_trusted_inputs_fail_closed_before_classifier(monkeypatch, override):
    from src import gateway_tool_intent as service
    classifier = MagicMock(side_effect=AssertionError("classifier called"))
    monkeypatch.setattr(service, "selected_tools_for_request", classifier)
    grant, sess, p = args()
    p.update(override)
    with pytest.raises(AdmissionDenied):
        select_admitted_tool_intent(grant, sess, **p)
    classifier.assert_not_called()


def test_invalid_history_result_fails_closed_without_contract_resolution(monkeypatch):
    from src import gateway_tool_intent as service
    monkeypatch.setattr(service, "selected_tools_for_request", lambda _: None)
    grant, sess, p = args(
        contextual_web_followup=True, recent_web_tool="manage_memory",
    )
    with pytest.raises(AdmissionDenied):
        select_admitted_tool_intent(grant, sess, **p)


def test_service_has_no_web_fastapi_or_dispatcher_imports():
    from src import gateway_tool_intent

    tree = ast.parse(inspect.getsource(gateway_tool_intent))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette", "src.tool_execution"))
        if isinstance(node, ast.Import):
            assert all(
                not imp.name.startswith(("routes", "fastapi", "starlette", "src.tool_execution"))
                for imp in node.names
            )


def test_web_handoff_is_single_and_before_immutable_resolution():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text()
    tree = ast.parse(source)
    matching = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "select_tool_intent"
    ]
    assert len(matching) == 1
    keywords = {kw.arg: kw.value for kw in matching[0].keywords}
    assert isinstance(keywords["context_owner"], ast.Attribute)
    assert keywords["context_owner"].attr == "user"
    assert isinstance(keywords["inventory"], ast.Name)
    assert keywords["inventory"].id == "_tool_inventory"
    assert isinstance(keywords["recent_web_tool_lookup"], ast.Lambda)
    assert source.index("gateway_turns.prepare_tool_inventory(") < source.index(
        "gateway_turns.select_tool_intent("
    ) < source.index("gateway_turns.resolve_routed_contract(")
    assert "preserve_bound_editor_selected_tools(" not in source
    assert "requests_independent_web_source(" not in source
