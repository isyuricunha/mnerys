"""M2.1 Gateway approval decisions: owner-bound, destructive only when valid.

These tests use the inherited sealed ToolApprovalStore and no real tool calls.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
)
from src.gateway_turns import GatewayTurns
from src.session_repository import SessionOwnership
from src.tool_approval_scopes import ToolApprovalScope
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action


def _admit(owner="alice", session="thread-1", kind="browser"):
    scopes = frozenset({"chat"}) if kind == "bearer" else frozenset()
    return GatewayAdmission().admit(
        Principal(owner, kind, scopes),
        session,
        lambda _: SessionOwnership(owner),
    )


def _pending(store, owner="alice", session="thread-1", *, tainted=True):
    return store.create(
        owner=owner,
        session_id=session,
        origin_run_id="run-1",
        tool_name="manage_notes",
        content='{"action":"list"}',
        workspace=None,
        external_untrusted_context_seen=tainted,
        capabilities=capabilities_for_action(
            "manage_notes", '{"action":"list"}'
        ),
        request_authority=RequestAuthority.empty(
            owner=owner, session_id=session
        ),
    )


def _decide(
    store, pending, *, admission=None, decision="approve", plan_mode=False,
    internal=False,
):
    return GatewayTurns(Mock()).decide_tool_approval(
        admission or _admit(),
        pending.approval_id,
        decision,
        plan_mode=plan_mode,
        internal_tool_request=internal,
        store=store,
    )


@pytest.mark.parametrize(
    "choice,expected_scope",
    [
        ("approve", ToolApprovalScope.CHAT_SESSION),
        ("APPROVE_TASK", ToolApprovalScope.TASK),
        ("  deny  ", None),
    ],
)
def test_decision_consumes_only_exact_matching_pending_card(choice, expected_scope):
    store = ToolApprovalStore()
    pending = _pending(store)
    result = _decide(store, pending, decision=choice)
    assert result.pending is pending
    assert result.decision == choice.strip().lower()
    assert result.tainted
    assert store.peek(pending.approval_id) is None
    if expected_scope is None:
        assert result.exact is None
    else:
        assert result.exact is not None
        assert result.exact.pending is pending
        assert result.exact.scope is expected_scope
        assert not result.exact._claimed


def test_projected_web_always_allow_settlement_still_saves_exact_rule(monkeypatch):
    from types import SimpleNamespace
    from src.gateway_approval_decisions import consume_admitted_tool_approval
    from src import tool_approval_rules

    store = ToolApprovalStore()
    pending = _pending(store)
    records = Mock()
    records.settle_web_approval.return_value = True
    saved = SimpleNamespace(rule_id="one-exact-rule")
    persist = Mock(return_value=saved)
    monkeypatch.setattr(tool_approval_rules, "persist_scoped_rule", persist)

    decision = consume_admitted_tool_approval(
        _admit(), pending.approval_id, "always_allow",
        plan_mode=False, internal_tool_request=False,
        store=store, records=records,
        session=SimpleNamespace(incarnation_id="epoch-a"),
    )
    assert decision.saved_rule is saved
    assert decision.exact is not None
    assert records.settle_web_approval.call_count == 1
    assert not records.record_approval.called
    assert persist.call_count == 1
    assert persist.call_args.kwargs["origin_approval_id"] == pending.approval_id
    assert persist.call_args.kwargs["content"] == pending.content


def test_retry_cannot_consume_a_second_time():
    store = ToolApprovalStore()
    pending = _pending(store)
    first = _decide(store, pending)
    assert first.exact is not None
    with pytest.raises(AdmissionDenied) as error:
        _decide(store, pending)
    assert error.value.status_code == 409


def test_untrusted_context_is_carried_from_sealed_record_not_ui():
    store = ToolApprovalStore()
    pending = _pending(store, tainted=False)
    result = _decide(store, pending)
    assert result.tainted is False


@pytest.mark.parametrize(
    "decision,plan_mode,code",
    [
        ("unknown", False, 400),
        (None, False, 400),
        ("approve", True, 409),
        ("deny", True, 409),
    ],
)
def test_invalid_decision_or_plan_mode_does_not_consume(
    decision, plan_mode, code,
):
    store = ToolApprovalStore()
    pending = _pending(store)
    with pytest.raises(AdmissionDenied) as error:
        _decide(store, pending, decision=decision, plan_mode=plan_mode)
    assert error.value.status_code == code
    assert store.peek(pending.approval_id) is pending


@pytest.mark.parametrize("other_owner,other_session", [
    ("mallory", "thread-1"),
    ("alice", "other-thread"),
])
def test_foreign_owner_or_thread_does_not_invalidate_original_card(
    other_owner, other_session,
):
    store = ToolApprovalStore()
    pending = _pending(store)
    with pytest.raises(AdmissionDenied) as error:
        _decide(
            store, pending,
            admission=_admit(owner=other_owner, session=other_session),
        )
    assert error.value.status_code == 409
    assert store.peek(pending.approval_id) is pending


def test_expired_or_unknown_card_is_rejected_without_grant(monkeypatch):
    import src.tool_approvals as approvals

    store = ToolApprovalStore(ttl_seconds=1)
    pending = _pending(store)
    monkeypatch.setattr(approvals.time, "time", lambda: pending.expires_at + 1)
    with pytest.raises(AdmissionDenied) as error:
        _decide(store, pending)
    assert error.value.status_code == 409
    assert store.peek(pending.approval_id) is None


@pytest.mark.parametrize("kind,internal", [
    ("bearer", False),
    ("browser", True),
])
def test_delegated_credentials_and_internal_calls_rejected_before_peek(
    kind, internal, monkeypatch,
):
    store = ToolApprovalStore()
    pending = _pending(store)
    monkeypatch.setattr(
        store, "peek",
        lambda *_args: pytest.fail("Restricted request inspected approval"),
    )
    with pytest.raises(AdmissionDenied) as error:
        _decide(
            store, pending, admission=_admit(kind=kind), internal=internal,
        )
    assert error.value.status_code == 403


def test_forged_admission_is_rejected_before_peek(monkeypatch):
    store = ToolApprovalStore()
    pending = _pending(store)
    monkeypatch.setattr(
        store, "peek",
        lambda *_args: pytest.fail("Forged admission inspected approval"),
    )
    with pytest.raises(AdmissionDenied) as error:
        _decide(
            store, pending,
            admission=AdmittedSession(
                Principal("alice", "browser"), "thread-1"
            ),
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "bad_plan,bad_internal",
    [("false", False), (False, "false")],
)
def test_untrusted_server_flags_fail_closed(bad_plan, bad_internal):
    store = ToolApprovalStore()
    pending = _pending(store)
    with pytest.raises(AdmissionDenied) as error:
        GatewayTurns(Mock()).decide_tool_approval(
            _admit(), pending.approval_id, "approve",
            plan_mode=bad_plan, internal_tool_request=bad_internal,
            store=store,
        )
    assert error.value.status_code == 403
    assert store.peek(pending.approval_id) is pending


def test_expiry_between_peek_and_consume_is_a_conflict(monkeypatch):
    store = ToolApprovalStore()
    pending = _pending(store)
    monkeypatch.setattr(store, "consume", lambda *_a, **_kw: None)
    with pytest.raises(AdmissionDenied) as error:
        _decide(store, pending)
    assert error.value.status_code == 409


def test_single_user_mode_preserves_legacy_empty_owner_semantics():
    store = ToolApprovalStore()
    pending = _pending(store, owner=None)
    admission = GatewayAdmission().admit(
        Principal(None, "single_user"),
        "thread-1", lambda _: SessionOwnership(None),
    )
    result = _decide(store, pending, admission=admission)
    assert result.exact is not None
    assert result.pending.owner == ""


def test_web_adapter_keeps_transport_only_guards_and_resolution_side_effects():
    route = (
        Path(__file__).resolve().parents[1] / "routes/chat_routes.py"
    ).read_text(encoding="utf-8")
    assert "require_user_approval_request(request)" in route
    assert "_reject_delegated_tool_approval(request)" in route
    assert "gateway_turns.decide_tool_approval(" in route
    assert "approval_resolution.pending" in route
    assert "approval_resolution.tainted" in route
    assert "_mark_tool_approval_resolved(" in route
    assert "tool_approval_store.consume(" not in route
