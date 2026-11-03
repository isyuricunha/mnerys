"""M2.1: consumed approvals cannot silently mint or transfer turn authority.

The real in-memory store consumes exact decisions. The Gateway only derives
the continuation authority; the existing dispatcher still claims the action.
"""

from dataclasses import replace

import pytest

from src.agent_runtime.authority import RequestAuthority, ExactOperation
from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal
from src.gateway_turns import GatewayTurns
from src.session_repository import SessionOwnership
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.tool_policy import ToolPolicy


def _admit(kind="browser", owner="alice"):
    scopes = frozenset({"chat"}) if kind == "bearer" else frozenset()
    return GatewayAdmission().admit(
        Principal(owner, kind, scopes), "owned-session",
        lambda _: SessionOwnership(owner),
    )


def _build(admission, exact, *, workspace=None, internal=False, policy=None):
    return GatewayTurns(None).build_authority(
        admission, "Run bash in the workspace",
        workspace=workspace, history=(),
        policy=policy if policy is not None else ToolPolicy(),
        internal_tool_request=internal, exact_approval=exact,
    )


def _consumed(*, owner="alice", workspace=None, original=True):
    admission = _admit(owner=owner)
    baseline = _build(admission, None, workspace=workspace)
    store = ToolApprovalStore()
    pending = store.create(
        owner=owner, session_id="owned-session", origin_run_id="run-original",
        tool_name="bash", content="pwd", workspace=workspace,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "pwd"),
        request_authority=baseline if original else None,
    )
    exact = store.consume(
        pending.approval_id, decision="approve",
        owner=owner, session_id="owned-session",
    )
    assert exact is not None
    assert store.peek(pending.approval_id) is None
    return exact, admission


def _shell():
    return ExactOperation.normalize("bash", "pwd")


def test_consumed_approval_reuses_originating_request_without_minting(monkeypatch):
    exact, admission = _consumed()
    from src import gateway_turn_authority as module

    monkeypatch.setattr(
        module, "create_request_authority",
        lambda *_args, **_kwargs: pytest.fail("Approval minted a fresh user grant"),
    )
    authority = _build(admission, exact)
    assert authority.request_id == exact.pending.request_authority.request_id
    assert authority.grants == exact.pending.request_authority.grants
    assert authority.bound_to(owner="alice", session_id="owned-session")
    assert authority.permits(_shell())
    assert exact._claimed is False  # Dispatcher, not Gateway, claims exact actions.


def test_consumed_approval_still_honors_current_server_disabled_tools():
    exact, admission = _consumed()
    policy = ToolPolicy(disabled_tools=frozenset({"bash"}))
    result = _build(admission, exact, policy=policy)
    assert not result.permits(_shell())
    assert "bash" in result.denied


def test_legacy_approval_without_originating_authority_is_empty():
    exact, admission = _consumed(original=False)
    result = _build(admission, exact)
    assert result.grants == ()
    assert not result.permits(_shell())


@pytest.mark.parametrize(
    "alter",
    [
        "owner",
        "session",
        "workspace",
        "wrong_request_owner",
        "wrong_request_session",
        "wrong_request_workspace",
    ],
)
def test_approval_bindings_fail_closed(alter):
    exact, admission = _consumed()
    if alter == "owner":
        exact.pending = replace(exact.pending, owner="mallory")
    elif alter == "session":
        exact.pending = replace(exact.pending, session_id="other-session")
    elif alter == "workspace":
        exact.pending = replace(exact.pending, workspace="/not-the-selected-workspace")
    elif alter == "wrong_request_owner":
        exact.pending = replace(
            exact.pending, request_authority=RequestAuthority.empty(
                owner="mallory", session_id="owned-session",
            ),
        )
    elif alter == "wrong_request_session":
        exact.pending = replace(
            exact.pending, request_authority=RequestAuthority.empty(
                owner="alice", session_id="other-session",
            ),
        )
    elif alter == "wrong_request_workspace":
        exact.pending = replace(
            exact.pending, request_authority=RequestAuthority.empty(
                owner="alice", session_id="owned-session",
                workspace="/not-the-selected-workspace",
            ),
        )
    with pytest.raises(AdmissionDenied) as denied:
        _build(admission, exact)
    assert denied.value.status_code == 409


@pytest.mark.parametrize("kind,internal", [("bearer", False), ("browser", True)])
def test_approval_never_becomes_a_delegated_or_internal_grant(kind, internal):
    exact, _ = _consumed()
    with pytest.raises(AdmissionDenied) as denied:
        _build(_admit(kind), exact, internal=internal)
    assert denied.value.status_code == 403


def test_untrusted_approval_object_is_rejected():
    with pytest.raises(AdmissionDenied) as denied:
        _build(_admit(), object())
    assert denied.value.status_code == 403


def test_approval_workspace_normalization_matches_store_behavior(tmp_path):
    normalized = str(tmp_path)
    equivalent = str(tmp_path / "folder" / "..")
    exact, admission = _consumed(workspace=normalized)
    assert _build(admission, exact, workspace=equivalent).bound_to(
        owner="alice", session_id="owned-session", workspace=normalized
    )



def test_approval_from_different_admitted_owner_is_rejected():
    exact, _ = _consumed(owner="alice")
    with pytest.raises(AdmissionDenied) as denied:
        _build(_admit(owner="bob"), exact)
    assert denied.value.status_code == 409


def test_approval_for_another_workspace_is_rejected(tmp_path):
    chosen = tmp_path / "chosen"
    other = tmp_path / "other"
    exact, admission = _consumed(workspace=str(chosen))
    with pytest.raises(AdmissionDenied) as denied:
        _build(admission, exact, workspace=str(other))
    assert denied.value.status_code == 409


def test_block_all_policy_prevents_approved_tool():
    exact, admission = _consumed()
    result = _build(
        admission, exact, policy=ToolPolicy(block_all_tool_calls=True),
    )
    assert result.block_all is True
    assert not result.permits(_shell())
