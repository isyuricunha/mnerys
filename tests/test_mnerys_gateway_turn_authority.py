"""M2.1 boundary: a trusted admission must precede user-intent tool authority."""
from dataclasses import replace

import pytest

from src.agent_runtime.authority import (
    ExactOperation,
    RequestAuthority,
    create_request_authority,
)
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
)
from src.gateway_turn_authority import authority_for_admitted_turn
from src.session_repository import SessionOwnership
from src.tool_policy import ToolPolicy


def admitted(kind="browser", owner="alice"):
    scopes = frozenset({"chat"}) if kind == "bearer" else frozenset()
    return GatewayAdmission().admit(
        Principal(owner, kind, scopes),
        "owned-session",
        lambda _: SessionOwnership(owner),
    )


def mint(grant, *, text="Run bash in the workspace", policy=None, internal=False):
    return authority_for_admitted_turn(
        grant,
        text,
        workspace=None,
        history=(),
        policy=policy if policy is not None else ToolPolicy(),
        internal_tool_request=internal,
    )


def shell():
    return ExactOperation.normalize("bash", "pwd")


def test_admitted_browser_preserves_legacy_authority_grants():
    grant = admitted()
    result = mint(grant)
    previous = create_request_authority(
        "Run bash in the workspace",
        owner="alice", session_id="owned-session",
        workspace=None, history=(), policy=ToolPolicy(),
        client_runtime_context=None, active_document=False,
        image_attachment=False, capabilities=(),
    )
    assert result.bound_to(owner="alice", session_id="owned-session", workspace=None)
    assert result.grants == previous.grants
    assert result.denied == previous.denied
    assert result.permits(shell())


def test_delegated_token_cannot_use_owner_interactive_shell_grant():
    result = mint(admitted("bearer"))
    assert result.bound_to(owner="alice", session_id="owned-session", workspace=None)
    assert not result.permits(shell())
    assert "bash" in result.denied


def test_server_tool_policy_still_denies_selected_intent():
    policy = ToolPolicy(disabled_tools=frozenset({"bash"}))
    authority = mint(admitted(), policy=policy)
    assert not authority.permits(shell())
    assert "bash" in authority.denied


@pytest.mark.parametrize("kind", ["browser", "bearer"])
def test_internal_tool_continuation_never_mints_new_grants(kind):
    authority = mint(admitted(kind), internal=True)
    assert isinstance(authority, RequestAuthority)
    assert authority.grants == ()
    assert not authority.permits(shell())


@pytest.mark.parametrize("tamper", ["none", "unissued", "session", "owner", "proof"])
def test_unauthorized_admission_fails_before_building_authority(monkeypatch, tamper):
    from src import gateway_turn_authority as module

    original = admitted()
    fake = {
        "none": None,
        "unissued": AdmittedSession(original.principal, original.session_id),
        "session": replace(original, session_id="different-session"),
        "owner": replace(original, principal=Principal("other", "browser")),
        "proof": replace(original, _proof=object()),
    }[tamper]
    monkeypatch.setattr(
        module,
        "create_request_authority",
        lambda *_args, **_kwargs: pytest.fail("Attempted authority mint"),
    )
    with pytest.raises(AdmissionDenied) as exc:
        mint(fake)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("policy,internal", [(None, "false"), (object(), False)])
def test_malformed_server_context_fails_closed(policy, internal):
    with pytest.raises(AdmissionDenied) as exc:
        authority_for_admitted_turn(
            admitted(), "Run bash", workspace=None, history=(),
            policy=policy, internal_tool_request=internal,
        )
    assert exc.value.status_code == 403


def test_unscoped_bearer_rejected_before_authority_builder():
    with pytest.raises(AdmissionDenied) as exc:
        GatewayAdmission().admit(
            Principal("alice", "bearer", frozenset({"notes"})),
            "owned-session",
            lambda _: SessionOwnership("alice"),
        )
    assert exc.value.status_code == 403


def test_gateway_facade_builds_authority_without_run_side_effects():
    from unittest.mock import MagicMock
    from src.gateway_turns import GatewayTurns

    runs = MagicMock()
    gateway = GatewayTurns(runs)
    grant = gateway.build_authority(
        admitted(), "Run bash in the workspace",
        workspace=None, history=(), policy=ToolPolicy(),
        internal_tool_request=False,
    )
    assert grant.bound_to(owner="alice", session_id="owned-session", workspace=None)
    assert grant.permits(shell())
    runs.assert_not_called()


def test_gateway_facade_refuses_unadmitted_authority():
    from unittest.mock import MagicMock
    from src.gateway_turns import GatewayTurns

    runs = MagicMock()
    gateway = GatewayTurns(runs)
    with pytest.raises(AdmissionDenied):
        gateway.build_authority(
            AdmittedSession(Principal("alice", "browser"), "owned-session"),
            "Run bash in the workspace",
            workspace=None, history=(), policy=ToolPolicy(),
            internal_tool_request=False,
        )
    runs.assert_not_called()
