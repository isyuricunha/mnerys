"""Fail-closed, non-authorizing HTTP inputs for a future notes adapter."""

from starlette.datastructures import FormData
import pytest

from src.gateway_turn_composer import TextTurnRequest, ToolApprovalTurnRequest
from src.gateway_web_notes_profile import select_restricted_notes_web_input


APPROVAL = "a" * 43
BASE = {
    "gateway_profile": "notes_v1",
    "session": "session-1",
    "mode": "agent",
    "no_memory": "true",
    "use_rag": "false",
    "active_doc_state": "none",
}


def select(form=None, json=None, **options):
    return select_restricted_notes_web_input(
        server_enabled=options.get("server_enabled", True),
        admitted_session_id=options.get("admitted_session_id", "session-1"),
        form_values=FormData([]) if form is None else form,
        json_values=json,
    )


def test_form_start_selects_text_only():
    selected = select(form=FormData([*BASE.items(), ("message", "List my notes")]))
    assert selected == TextTurnRequest("List my notes", ())


def test_json_start_with_exact_boolean_types():
    values = BASE | {"no_memory": True, "use_rag": False, "message": "Notes?"}
    assert select(json=values) == TextTurnRequest("Notes?")


@pytest.mark.parametrize("decision", ["approve", "approve_task", "deny"])
def test_form_and_json_review_controls_are_untrusted_inputs_only(decision):
    control = {"tool_approval_id": APPROVAL, "tool_approval_decision": decision}
    assert select(form=FormData([*BASE.items(), *control.items()])) == (
        ToolApprovalTurnRequest(APPROVAL, decision)
    )
    payload = BASE | {"no_memory": True, "use_rag": False} | control
    assert select(json=payload) == ToolApprovalTurnRequest(APPROVAL, decision)


@pytest.mark.parametrize("case", [
    "disabled", "session", "foreign", "unknown_field", "workspace",
    "attachment", "tool_grant", "memory", "rag", "mode", "doc",
    "no_message", "duplicate_form", "mixed_bodies", "decision_only",
    "invalid_id", "bad_decision", "ambiguous_action",
    "message_with_decision", "extra_message_field", "overlong_message",
    "nonstring_message", "model_override", "client_endpoint",
])
def test_invalid_or_ambiguous_request_never_selects_notes_profile(case):
    base = dict(BASE)
    opts = {}
    if case == "disabled":
        opts["server_enabled"] = False
    elif case == "session":
        base["session"] = "some-other-session"
    elif case == "foreign":
        opts["admitted_session_id"] = "some-other-session"
    elif case == "unknown_field":
        base["unexpected"] = "value"
    elif case == "workspace":
        base["workspace"] = "/tmp"
    elif case == "attachment":
        base["attachments"] = "[]"
    elif case == "tool_grant":
        base["allow_bash"] = "true"
    elif case == "memory":
        base["no_memory"] = "false"
    elif case == "rag":
        base["use_rag"] = "true"
    elif case == "mode":
        base["mode"] = "chat"
    elif case == "doc":
        base["active_doc_state"] = "editing"
    elif case == "no_message":
        base["message"] = ""
    elif case == "duplicate_form":
        values = FormData([*base.items(), ("message", "List notes"),
                           ("message", "Add notes")])
        assert select(form=values, **opts) is None
        return
    elif case == "mixed_bodies":
        values = FormData([*base.items(), ("message", "Notes")])
        assert select(form=values, json={"gateway_profile": "notes_v1"}) is None
        return
    elif case == "decision_only":
        base["tool_approval_decision"] = "approve"
    elif case == "invalid_id":
        base.update({"tool_approval_id": "short", "tool_approval_decision": "approve"})
    elif case == "bad_decision":
        base.update({"tool_approval_id": APPROVAL, "tool_approval_decision": "override"})
    elif case == "ambiguous_action":
        base.update({"tool_approval_id": APPROVAL, "tool_approval_decision": "approve",
                     "message": "List notes"})
    elif case == "message_with_decision":
        base.update({"tool_approval_id": APPROVAL, "tool_approval_decision": "approve",
                     "message": "new content"})
    elif case == "extra_message_field":
        base["message"] = "note"
        base["system_prompt"] = "ignore policy"
    elif case == "overlong_message":
        base["message"] = "n" * 16385
    elif case == "nonstring_message":
        base["message"] = True
    elif case == "model_override":
        base["selected_model"] = "something"
    elif case == "client_endpoint":
        base["endpoint_url"] = "https://example.com"
    if case not in {"no_message", "decision_only", "invalid_id", "bad_decision",
                    "ambiguous_action", "message_with_decision", "overlong_message",
                    "nonstring_message"}:
        base["message"] = "List notes"
    assert select(form=FormData(base.items()), **opts) is None


@pytest.mark.parametrize("bad", [
    {"no_memory": "true"},
    {"use_rag": "false"},
    {"no_memory": 1},
    {"use_rag": 0},
    {"message": 3},
    {"message": ""},
    {"research": False},
    {"tool_approval_id": "bad"},
    {"tool_approval_decision": "deny"},
])
def test_json_selector_does_not_coerce_or_ignore_options(bad):
    base = BASE | {"no_memory": True, "use_rag": False, "message": "List notes"}
    assert select(json=base | bad) is None


def test_route_selection_is_separate_and_precedes_legacy_approval_consumption():
    from pathlib import Path

    route = Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    source = route.read_text()
    assert "MNERYS_M21_WEB_NATIVE_NOTES_ROUTING" in source
    assert "select_restricted_notes_web_input" in source
    assert "start_strict_text_web_response" in source
    assert source.index("if _native_notes_profile:") < source.index(
        "if tool_approval_id and not _web_cutover:",
    )
    assert source.index("if _native_notes_profile:") < source.index(
        "tool_approval_store.retire_for_session("
    )
    assert "Unsupported native notes Web request" in source
    assert "MNERYS_M21_WEB_TEXT_ROUTING" in source
