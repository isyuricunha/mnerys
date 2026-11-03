"""M1 design-exit checks for the existing internal admission and run facade.

Browser and scoped-token *principal projections* exercise the same policy
contract here. They are not separate shipped transport adapters or a new
authenticated Gateway. These tests must not be presented as M2 acceptance.
"""

from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmissionDenied,
    AdmittedSession,
    GatewayAdmission,
    Principal,
)
from src.gateway_turns import GatewayTurns
from src.session_repository import SessionOwnership
from src.gateway_execution_policy import execution_limits_for_admission
from src.tool_security import delegated_credential_blocked_tools


def _owned_session(session_id):
    assert session_id == "example-session"
    return SessionOwnership(owner_id="owner-a")


@pytest.mark.parametrize(
    "principal",
    [
        Principal("owner-a", "browser"),
        Principal("owner-a", "bearer", frozenset({"chat"})),
    ],
)
def test_browser_and_token_projections_share_checked_run_facade(principal):
    """Both trusted projections reach the same internal run identity."""
    admitted = GatewayAdmission().admit(
        principal, "example-session", _owned_session
    )
    turns = MagicMock()
    gateway = GatewayTurns(turns)
    expected_run = "run-a"
    gateway.resume_active(admitted)
    gateway.stop(admitted, expected_run)
    gateway.finish(admitted, expected_run)
    gateway.is_active(admitted)

    turns.resume_active.assert_called_once_with("example-session")
    turns.stop.assert_called_once_with("example-session", expected_run)
    turns.finish.assert_called_once_with("example-session", expected_run)
    turns.is_active.assert_called_once_with("example-session")


@pytest.mark.parametrize(
    "principal,expected_status",
    [
        (Principal("owner-b", "browser"), 404),
        (Principal("owner-b", "bearer", frozenset({"chat"})), 404),
        (Principal("owner-a", "bearer", frozenset({"notes"})), 403),
        (Principal(None, "browser"), 401),
        (Principal(None, "bearer", frozenset({"chat"})), 403),
    ],
)
def test_untrusted_scope_and_foreign_owner_never_reach_run_control(
    principal, expected_status
):
    lookup_calls = []

    def lookup(session_id):
        lookup_calls.append(session_id)
        return _owned_session(session_id)

    with pytest.raises(AdmissionDenied) as raised:
        GatewayAdmission().admit(principal, "example-session", lookup)
    assert raised.value.status_code == expected_status
    assert lookup_calls == (
        [] if expected_status in (401, 403) else ["example-session"]
    )


@pytest.mark.parametrize(
    "principal",
    [
        Principal("owner-a", "browser"),
        Principal("owner-a", "bearer", frozenset({"chat"})),
    ],
)
def test_tampered_or_unadmitted_session_cannot_reach_run_control(principal):
    turns = MagicMock()
    gateway = GatewayTurns(turns)
    fabricated = AdmittedSession(principal, "example-session")
    with pytest.raises(AdmissionDenied):
        gateway.stop(fabricated, "run-a")
    with pytest.raises(AdmissionDenied):
        gateway.resume_active(None)
    assert turns.method_calls == []


def test_delegated_principal_cannot_gain_browser_tool_grants():
    browser = GatewayAdmission().admit(
        Principal("owner-a", "browser"),
        "example-session", _owned_session,
    )
    bearer = GatewayAdmission().admit(
        Principal("owner-a", "bearer", frozenset({"chat"})),
        "example-session", _owned_session,
    )
    restrictions = {
        "can_use_bash": False,
        "can_use_research": False,
        "can_use_agent": False,
    }
    browser_limits = execution_limits_for_admission(
        browser, restrictions, browser_tools=frozenset({"mcp__browser__open"}),
    )
    bearer_limits = execution_limits_for_admission(
        bearer, restrictions, browser_tools=frozenset({"mcp__browser__open"}),
    )
    assert bearer_limits.disabled_tools >= browser_limits.disabled_tools
    assert delegated_credential_blocked_tools() <= bearer_limits.disabled_tools
    assert browser_limits.allow_research is bearer_limits.allow_research is False
    assert browser_limits.allow_agent is bearer_limits.allow_agent is False
