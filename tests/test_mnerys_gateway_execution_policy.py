"""M1 server-owned privilege restrictions; the full tool contract remains inherited."""

from dataclasses import replace

import pytest

from src.gateway_admission import (
    AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_execution_policy import execution_limits_for_admission
from src.tool_security import delegated_credential_blocked_tools
from src.turn_contract import FAMILY_TOOLS


BROWSER_TOOLS = frozenset({"mcp__browser__open", "mcp__browser__click"})


def admitted(kind="browser"):
    principal = Principal(
        "alice", kind, frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "test-session", lambda _: SessionOwnership("alice"),
    )


def limits(kind="browser", privileges=None):
    return execution_limits_for_admission(
        admitted(kind), privileges, browser_tools=BROWSER_TOOLS,
    )


def test_unrestricted_browser_keeps_existing_default_permissions():
    result = limits()
    assert result.disabled_tools == frozenset()
    assert result.allow_research is True
    assert result.allow_agent is True


def test_delegated_token_is_restricted_even_when_owner_has_admin_privileges():
    granted = {key: True for key in (
        "can_use_bash", "can_use_browser", "can_use_documents",
        "can_generate_images", "can_manage_memory", "can_use_research",
        "can_use_agent",
    )}
    result = limits("bearer", granted)
    assert delegated_credential_blocked_tools() <= result.disabled_tools
    assert result.allow_agent is True


@pytest.mark.parametrize(("flag", "blocked"), [
    ("can_use_bash", FAMILY_TOOLS["shell_files"]),
    ("can_use_browser", BROWSER_TOOLS | {"private_browser"}),
    ("can_use_documents", {
        "manage_documents", "create_document", "edit_document",
        "update_document", "suggest_document",
    }),
    ("can_generate_images", {"generate_image", "edit_image"}),
    ("can_manage_memory", {"manage_memory", "manage_skills"}),
    ("can_use_research", {"trigger_research", "manage_research"}),
])
def test_account_privilege_denials_are_applied_to_every_required_tool(flag, blocked):
    result = limits(privileges={flag: False})
    assert set(blocked) <= result.disabled_tools
    if flag == "can_use_research":
        assert result.allow_research is False
    else:
        assert result.allow_research is True


def test_agent_privilege_denial_blocks_agent_mode():
    result = limits(privileges={"can_use_agent": False})
    assert result.allow_agent is False


def test_combined_denials_do_not_restore_delegated_tools():
    result = limits("bearer", {
        "can_use_bash": False,
        "can_use_documents": False,
        "can_use_agent": False,
        "can_use_research": False,
    })
    assert delegated_credential_blocked_tools() <= result.disabled_tools
    assert set(FAMILY_TOOLS["shell_files"]) <= result.disabled_tools
    assert "edit_document" in result.disabled_tools
    assert result.allow_agent is False
    assert result.allow_research is False


def test_malformed_server_privileges_fail_closed():
    with pytest.raises(AdmissionDenied) as denied:
        execution_limits_for_admission(
            admitted(), object(), browser_tools=BROWSER_TOOLS,
        )
    assert denied.value.status_code == 403


def test_fake_or_modified_admission_cannot_generate_execution_privileges():
    original = admitted()
    for forged in [None, replace(original, session_id="other")]:
        with pytest.raises(AdmissionDenied) as denied:
            execution_limits_for_admission(
                forged, {"can_use_bash": True}, browser_tools=BROWSER_TOOLS,
            )
        assert denied.value.status_code == 403
