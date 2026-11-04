"""M2.1 admitted session-dependent Chat-to-Agent promotion parity/denial.

The original Web ordering is independently represented in this test module;
all history, model, workspace and document facts are synthetic. No DB, actual
browser, path resolver or externally executable tools are needed.
"""

import ast
import inspect
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.action_intents import ToolIntent
from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_post_session_routing import (
    AdmittedDocumentEditRoute,
    AdmittedPostSessionRoute,
    promote_admitted_document_edit_route,
    reconcile_admitted_post_session_route,
)
from src.gateway_turns import GatewayTurns


def issued(owner="alice", kind="browser"):
    principal = Principal(
        None if kind == "single_user" else owner,
        kind,
        frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "session-1", lambda _: SessionOwnership(owner_id=owner),
    )


def loaded(admission):
    return SimpleNamespace(id=admission.session_id, owner=admission.principal.owner_id)


def args(
    *, owner="alice", kind="browser", message="Hello", chat_mode="chat",
    auto_escalated=False, tool_intent=None, workspace_agent_intent=False,
    workspace=None, allow_bash="false", explicit_browser_intent=False,
    clean_route_requested=False, recent_browser=False, web_followup=False,
    browser_followup=False, workspace_path=None,
):
    grant = issued(owner, kind)
    session = loaded(grant)
    callbacks = dict(
        recent_private_browser_success_lookup=MagicMock(return_value=recent_browser),
        contextual_web_followup_lookup=MagicMock(return_value=web_followup),
        contextual_browser_followup_lookup=MagicMock(return_value=browser_followup),
        workspace_from_message_lookup=MagicMock(return_value=(workspace_path, None)),
    )
    options = dict(
        context_owner=grant.principal.owner_id,
        message=message,
        chat_mode=chat_mode,
        auto_escalated=auto_escalated,
        tool_intent=tool_intent,
        workspace_agent_intent=workspace_agent_intent,
        workspace=workspace,
        allow_bash=allow_bash,
        explicit_browser_intent=explicit_browser_intent,
        clean_route_requested=clean_route_requested,
        **callbacks,
    )
    return grant, session, options


def old_route_reference(**options):
    """Transcription of the inherited Web branch, including short-circuits."""
    chat_mode = options["chat_mode"]
    auto_escalated = options["auto_escalated"]
    intent = options["tool_intent"]
    workspace_intent = options["workspace_agent_intent"]
    workspace = options["workspace"]
    allow_bash = options["allow_bash"]
    browser_intent = options["explicit_browser_intent"]
    clean = options["clean_route_requested"]
    message = options["message"]
    events = []

    warm = bool(clean and options["recent_private_browser_success_lookup"]())
    if warm:
        browser_intent = True
    if chat_mode == "chat" and clean:
        chat_mode = "agent"
        auto_escalated = True
        events.append("clean_route")
    if (
        chat_mode == "chat" and isinstance(message, str)
        and (not intent or not intent.needs_tools)
        and options["contextual_web_followup_lookup"]()
    ):
        intent = ToolIntent(True, "web", "contextual web lookup follow-up")
        chat_mode = "agent"
        auto_escalated = True
        workspace_intent = False
        events.append("contextual_web")
    browser_followup = bool(
        isinstance(message, str)
        and options["contextual_browser_followup_lookup"]()
    )
    if browser_followup:
        browser_intent = True
        if chat_mode == "chat":
            chat_mode = "agent"
            auto_escalated = True
            workspace_intent = False
            events.append("contextual_browser")
    if not workspace and isinstance(message, str):
        new_workspace, _ = options["workspace_from_message_lookup"]()
        if new_workspace:
            workspace = new_workspace
            chat_mode = "agent"
            auto_escalated = True
            workspace_intent = True
            allow_bash = "true"
            events.append("workspace_path")
    return (
        chat_mode, auto_escalated, intent, workspace_intent,
        workspace, allow_bash, browser_intent, warm, browser_followup,
        tuple(events),
    )


def clone_with_fresh_mocks(params):
    copy = dict(params)
    for key in (
        "recent_private_browser_success_lookup",
        "contextual_web_followup_lookup",
        "contextual_browser_followup_lookup",
        "workspace_from_message_lookup",
    ):
        copy[key] = MagicMock(return_value=params[key].return_value)
    return copy


@pytest.mark.parametrize("case", [
    {},
    {"message": None},
    {"message": 123},
    {"message": "", "clean_route_requested": True},
    {"chat_mode": "agent", "workspace_agent_intent": True},
    {"chat_mode": "agent", "clean_route_requested": True, "recent_browser": True},
    {"clean_route_requested": True, "recent_browser": True},
    {"clean_route_requested": True, "recent_browser": False},
    {"clean_route_requested": True, "recent_browser": True,
     "web_followup": True, "browser_followup": True},
    {"web_followup": True},
    {"web_followup": True, "tool_intent": ToolIntent(True, "notes", "add note")},
    {"web_followup": True, "tool_intent": ToolIntent(False)},
    {"web_followup": True, "workspace_agent_intent": True},
    {"web_followup": True, "browser_followup": True},
    {"browser_followup": True},
    {"browser_followup": True, "workspace_agent_intent": True},
    {"browser_followup": True, "explicit_browser_intent": True},
    {"browser_followup": True, "chat_mode": "agent"},
    {"workspace_path": "/workspace/report"},
    {"workspace_path": "/workspace/report", "chat_mode": "agent",
     "auto_escalated": False},
    {"workspace_path": "/workspace/report", "workspace_agent_intent": False,
     "allow_bash": "false"},
    {"workspace_path": "/workspace/report", "browser_followup": True,
     "web_followup": True},
    {"workspace": "/workspace/old", "workspace_path": "/workspace/new"},
    {"workspace": "", "workspace_path": "/workspace/new"},
    {"workspace": "/workspace/old", "message": None},
    {"tool_intent": ToolIntent(True, "shell", "run command"),
     "workspace_agent_intent": True, "auto_escalated": True},
    {"tool_intent": ToolIntent(True, "calendar", "appointment"),
     "web_followup": True},
    {"chat_mode": "agent", "allow_bash": None, "workspace_path": None},
])
def test_session_dependent_escalation_preserves_original_order(case):
    grant, sess, params = args(**case)
    original = clone_with_fresh_mocks(params)
    expected = old_route_reference(**original)
    result = reconcile_admitted_post_session_route(grant, sess, **params)
    assert isinstance(result, AdmittedPostSessionRoute)
    assert (
        result.chat_mode, result.auto_escalated, result.tool_intent,
        result.workspace_agent_intent, result.workspace, result.allow_bash,
        result.explicit_browser_intent, result.private_browser_warm,
        result.contextual_browser_followup, result.escalation_events,
    ) == expected

    for name in (
        "recent_private_browser_success_lookup",
        "contextual_web_followup_lookup",
        "contextual_browser_followup_lookup",
        "workspace_from_message_lookup",
    ):
        assert params[name].call_count == original[name].call_count, name
    assert not hasattr(result, "authority")
    assert not hasattr(result, "grants")


@pytest.mark.parametrize("kind", ["browser", "bearer", "single_user"])
def test_facade_uses_same_admission_for_all_supported_credentials(kind):
    grant, sess, params = args(kind=kind, browser_followup=True)
    direct = reconcile_admitted_post_session_route(grant, sess, **params)
    facade = GatewayTurns(MagicMock()).reconcile_post_session_route(
        grant, sess, **params,
    )
    assert direct == facade
    assert direct.chat_mode == "agent"


def test_workspace_path_has_final_precedence_without_replacing_owner_policy():
    grant, sess, params = args(
        clean_route_requested=True, recent_browser=True,
        browser_followup=True, web_followup=True,
        workspace_path="/workspace/report", allow_bash="false",
    )
    result = reconcile_admitted_post_session_route(grant, sess, **params)
    assert result.chat_mode == "agent"
    assert result.workspace_agent_intent
    assert result.allow_bash == "true"  # legacy routing hint only
    assert result.escalation_events == ("clean_route", "workspace_path")
    assert not hasattr(result, "tool_policy")


def test_browser_history_only_runs_for_a_text_turn_and_uses_typed_evidence():
    grant, sess, params = args(
        message=None, clean_route_requested=False, browser_followup=True,
    )
    result = reconcile_admitted_post_session_route(grant, sess, **params)
    assert not result.contextual_browser_followup
    for key in (
        "recent_private_browser_success_lookup",
        "contextual_web_followup_lookup",
        "contextual_browser_followup_lookup",
        "workspace_from_message_lookup",
    ):
        params[key].assert_not_called()


_DOCUMENT_PATTERN = re.compile(
    r"\b(?:make|sound|rewrite|revise|rework|edit|update|change|polish|"
    r"professional|fun|formal|casual|shorter|longer|friendlier|warmer|clearer)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("case", [
    {"message": "Rewrite this paragraph", "active_document": True},
    {"message": "Make it warmer", "active_document": True},
    {"message": "Please polish the open draft", "active_document": True},
    {"message": "This is formal English", "active_document": True},
    {"message": "Explain the title", "active_document": True},
    {"message": "rewrite", "active_document": False},
    {"message": None, "active_document": True},
    {"message": "Rewrite", "active_document": True, "chat_mode": "agent"},
    {"message": "Revise it", "active_document": True, "auto_escalated": True},
    {"message": "Calendar task", "active_document": True},
])
def test_document_edit_promotion_preserves_original_visible_tab_rule(case):
    grant, sess, _ = args()
    inputs = dict(
        context_owner=grant.principal.owner_id,
        message=case["message"],
        chat_mode=case.get("chat_mode", "chat"),
        auto_escalated=case.get("auto_escalated", False),
        active_document=case["active_document"],
    )
    edit = bool(
        inputs["active_document"] and inputs["chat_mode"] == "chat"
        and isinstance(inputs["message"], str)
        and _DOCUMENT_PATTERN.search(inputs["message"])
    )
    result = promote_admitted_document_edit_route(grant, sess, **inputs)
    assert isinstance(result, AdmittedDocumentEditRoute)
    assert result.edit_requested is edit
    assert result.chat_mode == ("agent" if edit else inputs["chat_mode"])
    assert result.auto_escalated == (inputs["auto_escalated"] or edit)
    assert GatewayTurns(MagicMock()).promote_document_edit_route(
        grant, sess, **inputs,
    ) == result
    assert not hasattr(result, "document_id")
    assert not hasattr(result, "write_grant")


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
])
def test_bad_session_denied_before_model_history_or_workspace_ports(tamper):
    grant, sess, params = args(clean_route_requested=True)
    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session_id":
        grant = replace(grant, session_id="foreign")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded_session":
        sess.id = "foreign"
    elif tamper == "loaded_owner":
        sess.owner = "bob"
    elif tamper == "context_owner":
        params["context_owner"] = "bob"
    with pytest.raises(AdmissionDenied) as error:
        reconcile_admitted_post_session_route(grant, sess, **params)
    assert error.value.status_code == 403
    for key in (
        "recent_private_browser_success_lookup",
        "contextual_web_followup_lookup",
        "contextual_browser_followup_lookup",
        "workspace_from_message_lookup",
    ):
        params[key].assert_not_called()


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "foreign", "owner",
])
def test_document_promotion_rejects_invalid_admission(tamper):
    grant, sess, _ = args()
    kw = dict(
        context_owner="alice", message="Rewrite this", chat_mode="chat",
        auto_escalated=False, active_document=True,
    )
    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "foreign":
        sess.id = "foreign"
    elif tamper == "owner":
        kw["context_owner"] = "foreign"
    with pytest.raises(AdmissionDenied) as error:
        promote_admitted_document_edit_route(grant, sess, **kw)
    assert error.value.status_code == 403


@pytest.mark.parametrize("change", [
    {"chat_mode": 10},
    {"auto_escalated": "false"},
    {"tool_intent": object()},
    {"workspace_agent_intent": 1},
    {"workspace": 42},
    {"explicit_browser_intent": "true"},
    {"clean_route_requested": None},
    {"recent_private_browser_success_lookup": False},
    {"contextual_web_followup_lookup": None},
    {"contextual_browser_followup_lookup": True},
    {"workspace_from_message_lookup": object()},
])
def test_invalid_session_routing_inputs_fail_before_lookup(change):
    grant, sess, params = args()
    params.update(change)
    with pytest.raises(AdmissionDenied):
        reconcile_admitted_post_session_route(grant, sess, **params)
    for key in (
        "recent_private_browser_success_lookup",
        "contextual_web_followup_lookup",
        "contextual_browser_followup_lookup",
        "workspace_from_message_lookup",
    ):
        if isinstance(params[key], MagicMock):
            params[key].assert_not_called()


@pytest.mark.parametrize("key,case,malformed", [
    ("recent_private_browser_success_lookup", {"clean_route_requested": True}, "true"),
    ("contextual_web_followup_lookup", {}, "true"),
    ("contextual_browser_followup_lookup", {}, 1),
    ("workspace_from_message_lookup", {}, "workspace"),
    ("workspace_from_message_lookup", {}, (42, None)),
    ("workspace_from_message_lookup", {}, ("/workspace/a",)),
])
def test_malformed_server_evidence_fails_closed(key, case, malformed):
    grant, sess, params = args(**case)
    params[key].return_value = malformed
    with pytest.raises(AdmissionDenied):
        reconcile_admitted_post_session_route(grant, sess, **params)


@pytest.mark.parametrize("change", [
    {"chat_mode": False},
    {"auto_escalated": 1},
    {"active_document": "true"},
])
def test_document_promotion_rejects_invalid_flags(change):
    grant, sess, _ = args()
    options = dict(
        context_owner="alice", message="Rewrite this", chat_mode="chat",
        auto_escalated=False, active_document=True,
    )
    options.update(change)
    with pytest.raises(AdmissionDenied):
        promote_admitted_document_edit_route(grant, sess, **options)


def test_gateway_is_transport_independent_and_does_not_import_executors():
    from src import gateway_post_session_routing

    tree = ast.parse(inspect.getsource(gateway_post_session_routing))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith((
                "routes", "fastapi", "starlette", "src.tool_execution",
                "src.agent_loop",
            ))
        elif isinstance(node, ast.Import):
            assert all(
                not entry.name.startswith((
                    "routes", "fastapi", "starlette", "src.tool_execution",
                    "src.agent_loop",
                ))
                for entry in node.names
            )


def test_web_handoffs_are_singular_admitted_and_ordered():
    root = Path(__file__).resolve().parents[1]
    source = (root / "routes/chat_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for method in (
        "reconcile_post_session_route", "promote_document_edit_route",
    ):
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "gateway_turns"
            and node.func.attr == method
        ]
        assert len(calls) == 1
        keywords = {kw.arg: kw.value for kw in calls[0].keywords}
        assert isinstance(keywords["context_owner"], ast.Name if method == "reconcile_post_session_route" else ast.Attribute)
        assert "context_owner" in keywords
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.index("_admitted_session = _verify_session_owner(") < stream.index(
        "gateway_turns.reconcile_post_session_route("
    ) < stream.index("build_chat_context(") < stream.index(
        "gateway_turns.promote_document_edit_route("
    ) < stream.index("gateway_turns.finalize_tool_policy(")
    assert "context_owner=owner," in stream
    assert "context_owner=ctx.user," in stream
    assert "recent_private_browser_success_lookup=lambda:" in stream
    assert "workspace_from_message_lookup=lambda:" in stream
    assert "active_document=bool(active_doc)" in stream
    assert "ToolIntent(True, \"web\", \"contextual web lookup follow-up\")" not in stream
