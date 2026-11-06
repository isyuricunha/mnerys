"""Production router selection checks for the gated native notes profile.

Only the explicitly opted-in request reaches the original Gateway adapter.
These route tests use synthetic sessions and do not enable deployment flags.
"""

from types import SimpleNamespace
import pytest
from fastapi import HTTPException

import routes.chat_routes as chat_routes
from src.gateway_turn_composer import TextTurnRequest, ToolApprovalTurnRequest
from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest


BASE = {
    "gateway_profile": "notes_v1", "session": "session-1",
    "mode": "agent", "no_memory": "true",
    "use_rag": "false", "active_doc_state": "none",
}
REVIEW = {"tool_approval_id": "a" * 43, "tool_approval_decision": "approve"}


def setup(monkeypatch, *, enabled=True):
    captured = {}
    endpoint = _chat_stream_endpoint(
        monkeypatch, "agent", captured, capture_context=True,
    )
    if enabled:
        monkeypatch.setenv("MNERYS_M21_WEB_NATIVE_NOTES_ROUTING", "1")
    else:
        monkeypatch.delenv("MNERYS_M21_WEB_NATIVE_NOTES_ROUTING", raising=False)
    monkeypatch.setattr("src.settings.get_setting", lambda *a, **k: [])
    monkeypatch.setattr(chat_routes, "fire_message_event", lambda *a, **k: None)
    # Route selection tests inspect the adapter handoff, not the DB runtime.
    # No configured app database is opened or migrated.
    monkeypatch.setattr(chat_routes, "server_notes_decision_fence", lambda: object())
    monkeypatch.setattr(chat_routes, "server_notes_event_journal", lambda: object())
    calls = []
    returned = object()

    def fake_adapter(**kwargs):
        calls.append(kwargs)
        return returned

    monkeypatch.setattr(
        chat_routes, "start_restricted_notes_web_response", fake_adapter,
    )
    return endpoint, captured, calls, returned


def request_with(values=None):
    request = _RouteRequest("agent")
    request._form = dict(BASE) | ({"message": "List my notes"} if values is None else values)
    return request


@pytest.mark.asyncio
async def test_opted_native_notes_route_checks_owner_model_and_policy_before_dispatch(monkeypatch):
    route, legacy, calls, result = setup(monkeypatch)
    request = request_with()
    checks = []
    original_verify = chat_routes._verify_session_owner
    original_limits = chat_routes.execution_limits_for_admission
    original_enforce = chat_routes._enforce_chat_privileges

    def checked_owner(req, session_id):
        checks.append("owner")
        return original_verify(req, session_id)

    def checked_limits(*args, **kwargs):
        checks.append("agent_policy")
        return original_limits(*args, **kwargs)

    def checked_privileges(*args, **kwargs):
        checks.append("model_policy")
        return original_enforce(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "_verify_session_owner", checked_owner)
    monkeypatch.setattr(chat_routes, "execution_limits_for_admission", checked_limits)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", checked_privileges)
    assert await route(request) is result
    assert checks == ["owner", "agent_policy", "model_policy"]
    assert len(calls) == 1
    assert calls[0]["request"] == TextTurnRequest("List my notes")
    assert calls[0]["admission"].session_id == "session-1"
    assert calls[0]["session"].id == "session-1"
    assert calls[0]["privileges"] == {}
    assert calls[0]["global_disabled_tools"] == ()
    assert callable(calls[0]["after_user_saved"])
    assert "build_context" not in legacy
    assert "agent" not in legacy


@pytest.mark.asyncio
async def test_disabled_profile_fails_closed_without_legacy_agent_or_consumption(monkeypatch):
    route, legacy, calls, _result = setup(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as err:
        await route(request_with())
    assert err.value.status_code == 403
    assert not calls and not legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", [
    {"allow_bash": "true"},
    {"selected_model": "different"},
    {"workspace": "/tmp"},
    {"attachments": "[]"},
    {"use_rag": "true"},
    {"gateway_profile": "notes_v1", "tool_approval_id": "a" * 43},
    {"gateway_profile": "notes_v1", "message": "x" * 16385},
    {"gateway_profile": "notes_v1", "mode": "chat"},
])
async def test_invalid_notes_never_falls_back_to_legacy_or_converts_to_approval(
    monkeypatch, extras,
):
    route, legacy, calls, _result = setup(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await route(request_with(extras))
    assert err.value.status_code == 400
    assert not calls and not legacy


@pytest.mark.asyncio
async def test_explicit_review_is_preconsumption_and_requires_human(monkeypatch):
    route, legacy, calls, result = setup(monkeypatch)
    approved = []
    monkeypatch.setattr(
        chat_routes, "_reject_delegated_tool_approval",
        lambda request: approved.append("not_delegated"),
    )
    import src.agent_runtime.authority as auth
    monkeypatch.setattr(
        auth, "require_user_approval_request",
        lambda request: approved.append("human_authorized"),
    )

    def legacy_consume(*args, **kwargs):
        raise AssertionError("Legacy tool approval must not be consumed")

    monkeypatch.setattr(chat_routes.gateway_turns, "decide_tool_approval", legacy_consume)
    monkeypatch.setattr(
        chat_routes.tool_approval_store, "retire_for_session",
        legacy_consume,
    )
    request = request_with(
        {"tool_approval_id": "a" * 43, "tool_approval_decision": "approve"},
    )
    assert await route(request) is result
    assert approved == ["not_delegated", "human_authorized"]
    assert calls[0]["request"] == ToolApprovalTurnRequest("a" * 43, "approve")
    assert "build_context" not in legacy


@pytest.mark.asyncio
async def test_delegated_review_rejected_before_original_approval_store(monkeypatch):
    route, legacy, calls, _result = setup(monkeypatch)

    def denied(req):
        raise HTTPException(403, "Delegated approval denied")

    monkeypatch.setattr(chat_routes, "_reject_delegated_tool_approval", denied)
    with pytest.raises(HTTPException) as err:
        await route(request_with({
            "tool_approval_id": "a" * 43, "tool_approval_decision": "deny",
        }))
    assert err.value.status_code == 403
    assert not calls and not legacy


@pytest.mark.asyncio
async def test_json_notes_request_preserves_exact_selector_without_form_coercion(monkeypatch):
    route, legacy, calls, result = setup(monkeypatch)
    request = request_with()
    request._form = {}
    request.headers["content-type"] = "application/json"
    async def json_body():
        return {
            "gateway_profile": "notes_v1", "session": "session-1",
            "mode": "agent", "no_memory": True,
            "use_rag": False, "active_doc_state": "none",
            "message": "Read my notes",
        }
    request.json = json_body
    assert await route(request) is result
    assert calls[0]["request"] == TextTurnRequest("Read my notes")
    assert "build_context" not in legacy


@pytest.mark.asyncio
async def test_duplicate_form_profile_cannot_fall_through_into_legacy_approval(monkeypatch):
    from starlette.datastructures import FormData

    route, legacy, calls, _result = setup(monkeypatch)
    request = request_with()
    request._form = FormData([
        *[(key, value) for key, value in BASE.items() if key != "gateway_profile"],
        ("gateway_profile", "notes_v1"),
        ("gateway_profile", "text_v1"),
        ("tool_approval_id", "a" * 43),
        ("tool_approval_decision", "approve"),
    ])
    with pytest.raises(HTTPException) as rejected:
        await route(request)
    assert rejected.value.status_code == 400
    assert not calls and not legacy


@pytest.mark.asyncio
async def test_selected_model_privileges_rechecked_after_server_route_reconciliation(
    monkeypatch,
):
    route, legacy, calls, _result = setup(monkeypatch)
    checks = []

    def reconcile(_req, sess, _session_id, _form, *, owner):
        assert owner == "alice"
        sess.model = "reconciled-model"
        checks.append("model_reconciled")

    def model_denied(_req, sess):
        assert sess.model == "reconciled-model"
        checks.append("checked_reconciled_model")
        raise HTTPException(403, "Model is outside account allowlist")

    monkeypatch.setattr(chat_routes, "_reconcile_selected_route_from_request", reconcile)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", model_denied)
    with pytest.raises(HTTPException) as err:
        await route(request_with())
    assert err.value.status_code == 403
    assert checks == ["model_reconciled", "checked_reconciled_model"]
    assert not calls and not legacy


@pytest.mark.asyncio
async def test_global_disable_is_forwarded_as_server_policy_not_client_toggle(monkeypatch):
    route, legacy, calls, result = setup(monkeypatch)
    monkeypatch.setattr("src.settings.get_setting", lambda *a, **k: ["manage_notes"])
    assert await route(request_with()) is result
    assert calls[0]["global_disabled_tools"] == ("manage_notes",)
    assert "agent" not in legacy
