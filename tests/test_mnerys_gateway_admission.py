"""M1: server-issued identity, session ownership and admitted run controls.

The principal adapter only consumes validated middleware request.state. These
tests do not pretend that HTTP headers supplied by clients authenticate users.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routes.gateway_principal import principal_for_chat_request
from src.gateway_admission import (
    AdmissionDenied,
    AdmittedSession,
    GatewayAdmission,
    Principal,
    SessionOwnership,
    require_admitted_session,
)
from src.gateway_turns import GatewayTurns


def request_state(**fields):
    return SimpleNamespace(state=SimpleNamespace(**fields))


def lookup(owner="alice"):
    calls = []

    def load(session_id):
        calls.append(session_id)
        return None if owner is _MISSING else SessionOwnership(owner_id=owner)

    return load, calls


_MISSING = object()


@pytest.mark.parametrize("credential,owner,scopes,expected", [
    ("browser", "alice", frozenset(), 200),
    ("browser", None, frozenset(), 401),
    ("bearer", "alice", frozenset({"chat"}), 200),
    ("bearer", "alice", frozenset({"notes"}), 403),
    ("bearer", None, frozenset({"chat"}), 403),
    ("single_user", None, frozenset(), 200),
    ("single_user", "alice", frozenset(), 401),
    ("unknown", "alice", frozenset(), 401),
])
def test_admission_rejects_invalid_principal_before_session_lookup(
    credential, owner, scopes, expected
):
    admission = GatewayAdmission()
    get_session, calls = lookup("alice")
    principal = Principal(owner, credential, scopes)
    if expected == 200:
        grant = admission.admit(principal, "s1", get_session)
        assert grant.session_id == "s1"
        assert require_admitted_session(grant) == "s1"
        assert calls == ["s1"]
    else:
        with pytest.raises(AdmissionDenied) as error:
            admission.admit(principal, "s1", get_session)
        assert error.value.status_code == expected
        assert calls == []


@pytest.mark.parametrize("owner,expected", [
    ("alice", 200),
    ("bob", 404),
    (None, 404),
    (_MISSING, 404),
])
def test_lookup_and_owner_isolation(owner, expected):
    get_session, calls = lookup(owner)
    actor = Principal("alice", "browser")
    if expected == 200:
        assert GatewayAdmission().admit(actor, "s1", get_session).session_id == "s1"
    else:
        with pytest.raises(AdmissionDenied) as error:
            GatewayAdmission().admit(actor, "s1", get_session)
        assert error.value.status_code == 404
    assert calls == ["s1"]


def test_single_user_mode_allows_legacy_owner_stamped_row_only_when_explicit():
    get_session, calls = lookup("legacy-owner")
    grant = GatewayAdmission().admit(
        Principal(None, "single_user"), "legacy", get_session,
    )
    assert grant.session_id == "legacy"
    assert calls == ["legacy"]


def test_invalid_session_ids_never_query_repository():
    get_session, calls = lookup()
    with pytest.raises(AdmissionDenied) as raised:
        GatewayAdmission().admit(Principal("alice", "browser"), "", get_session)
    assert raised.value.status_code == 404
    assert calls == []


def test_principal_adapter_rejects_unscoped_and_ownerless_bearer():
    without_chat = request_state(
        api_token=True, api_token_owner="alice", api_token_scopes=["tasks"],
        current_user="api",
    )
    without_owner = request_state(
        api_token=True, api_token_owner=None, api_token_scopes=["chat"],
        current_user="api",
    )
    for request in (without_chat, without_owner):
        with pytest.raises(HTTPException) as error:
            principal_for_chat_request(request)
        assert error.value.status_code == 403


def test_principal_adapter_separates_bearer_owner_from_execution_privilege(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    token = request_state(
        api_token=True, api_token_owner="alice", api_token_scopes=["chat"],
        current_user="api",
    )
    principal = principal_for_chat_request(token)
    assert principal.owner_id == "alice"
    assert principal.credential_kind == "bearer"
    assert principal.delegated is True
    assert principal.scopes == frozenset({"chat"})

    browser = principal_for_chat_request(request_state(
        api_token=False, current_user="alice",
    ))
    assert browser.owner_id == principal.owner_id
    assert browser.credential_kind == "browser"
    assert browser.delegated is False
    assert browser.scopes == frozenset()


def test_untrusted_anonymous_principal_does_not_get_single_user_mode(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    browser = principal_for_chat_request(request_state(
        api_token=False, current_user=None,
    ))
    assert browser.credential_kind == "browser"
    owner_lookup, calls = lookup("admin")
    with pytest.raises(AdmissionDenied) as raised:
        GatewayAdmission().admit(browser, "s1", owner_lookup)
    assert raised.value.status_code == 401
    assert calls == []


def test_explicit_auth_disabled_preserves_single_user_session_access(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    request = request_state(api_token=False, current_user=None)
    principal = principal_for_chat_request(request)
    assert principal.credential_kind == "single_user"
    get_session, _ = lookup("admin")
    assert GatewayAdmission().admit(principal, "sid", get_session).session_id == "sid"


@pytest.mark.parametrize("action", ["resume_active", "stop", "finish", "is_active", "start_detached"])
def test_unadmitted_session_cannot_reach_run_manager(action):
    turns = MagicMock()
    gateway = GatewayTurns(turns)
    for grant in (
        None,
        AdmittedSession(Principal("alice", "browser"), "s1"),
        "s1",
    ):
        with pytest.raises(AdmissionDenied) as raised:
            if action == "start_detached":
                gateway.start_detached(grant, object())
            elif action in {"stop", "finish"}:
                getattr(gateway, action)(grant, "run-id")
            else:
                getattr(gateway, action)(grant)
        assert raised.value.status_code == 403
    turns.assert_not_called()
    assert turns.method_calls == []


def test_copied_admission_cannot_change_owner_session_or_proof():
    original = GatewayAdmission().admit(
        Principal("alice", "browser"), "session-a",
        lambda _: SessionOwnership(owner_id="alice"),
    )
    turns = MagicMock()
    gateway = GatewayTurns(turns)
    for tampered in (
        replace(original, session_id="session-b"),
        replace(original, principal=Principal("bob", "browser")),
        replace(original, _proof=None),
    ):
        with pytest.raises(AdmissionDenied) as denied:
            gateway.stop(tampered, "run-id")
        assert denied.value.status_code == 403
    turns.assert_not_called()
    assert turns.method_calls == []


def test_admitted_gateway_controls_forward_only_checked_session_identity():
    turns = MagicMock()
    gateway = GatewayTurns(turns)
    grant = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "owner-sid",
        lambda _: SessionOwnership(owner_id="alice"),
    )
    assert gateway.resume_active(grant) is turns.resume_active.return_value
    assert gateway.is_active(grant) is turns.is_active.return_value
    gateway.stop(grant, "exact-run")
    gateway.finish(grant, "exact-run")
    source = object()
    gateway.start_detached(grant, source)

    turns.resume_active.assert_called_once_with("owner-sid")
    turns.is_active.assert_called_once_with("owner-sid")
    turns.stop.assert_called_once_with("owner-sid", "exact-run")
    turns.finish.assert_called_once_with("owner-sid", "exact-run")
    turns.start_detached.assert_called_once_with("owner-sid", source)
