"""M5.3 increment 2: the four Hermes-style approval decisions and scoped rules.

The four decisions — Allow Once, Allow for Session, Always Allow this specific
rule, Deny — are one shared Gateway mechanism. A scoped rule suppresses ONLY
the automatic approval PROMPT for the exact reviewed operation; every other
security gate (admission, tool policy, request authority, resource/CWD
binding, sealed exact-operation validation, read-before-edit, artifact
publication) still applies to every execution. Deny is the default when no
valid decision arrives.
"""
import json
import time
from pathlib import Path

import pytest

from src.gateway_admission import Principal

from src.gateway_admission import AdmissionDenied
from src.tool_approval_rules import (
    RULES_MAX_PER_OWNER,
    _ScopedRuleStore,
    bind_scoped_approval_rules,
)
from src.tool_approval_scopes import (
    ALLOW_ONCE_APPROVAL_DECISION,
    ALWAYS_ALLOW_APPROVAL_DECISION,
    CHAT_SESSION_APPROVAL_DECISION,
    DENY_APPROVAL_DECISION,
    TASK_APPROVAL_DECISION,
    ToolApprovalScope,
    scope_for_decision,
)
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action

OWNER = "alice"
SESSION = "owned-session"


def _caps(tool, content):
    return capabilities_for_action(tool, content)


@pytest.fixture(autouse=True)
def _isolated_rule_store(tmp_path, monkeypatch):
    store = _ScopedRuleStore()
    import src.tool_approval_rules as rules_module

    monkeypatch.setattr(rules_module, "_rules_path", lambda: str(tmp_path / "approval_rules.json"))
    reset = bind_scoped_approval_rules(store)
    yield store
    reset()


# ── Decision → scope mapping ──────────────────────────────────────────────

def test_allow_once_maps_to_single_action_scope():
    assert scope_for_decision(ALLOW_ONCE_APPROVAL_DECISION) is ToolApprovalScope.SINGLE_ACTION


def test_always_allow_grants_task_scope_for_the_immediate_action():
    assert scope_for_decision(ALWAYS_ALLOW_APPROVAL_DECISION) is ToolApprovalScope.TASK


def test_deny_and_unknown_have_no_scope():
    assert scope_for_decision(DENY_APPROVAL_DECISION) is None
    assert scope_for_decision("yolo") is None


def test_allow_once_never_widens_into_a_run_long_bypass():
    store = ToolApprovalStore()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="pwd",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "pwd"),
    )
    exact = store.consume(
        pending.approval_id, decision=ALLOW_ONCE_APPROVAL_DECISION,
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    assert exact is not None
    assert exact.scope is ToolApprovalScope.SINGLE_ACTION
    assert exact.allow_remaining_actions is False


def test_session_scope_still_allows_remaining_actions():
    store = ToolApprovalStore()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="pwd",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "pwd"),
    )
    exact = store.consume(
        pending.approval_id, decision=CHAT_SESSION_APPROVAL_DECISION,
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    assert exact.scope is ToolApprovalScope.CHAT_SESSION
    assert exact.allow_remaining_actions is True


def test_deny_consumes_without_granting():
    store = ToolApprovalStore()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="rm -rf old",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "rm -rf old"),
    )
    exact = store.consume(
        pending.approval_id, decision=DENY_APPROVAL_DECISION,
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    assert exact is None  # no authority, no claim, no execution
    assert store.peek(pending.approval_id) is None  # the prompt is resolved
    replay = store.consume(
        pending.approval_id, decision=ALLOW_ONCE_APPROVAL_DECISION,
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    assert replay is None  # a denied prompt cannot be revived into a grant


# ── The approval card offers the four decisions ───────────────────────────

def test_card_payload_offers_the_four_decisions():
    store = ToolApprovalStore()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="pwd",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "pwd"),
    )
    payload = pending.public_payload()
    values = [option["value"] for option in payload["options"]]
    assert values == [
        ALLOW_ONCE_APPROVAL_DECISION, TASK_APPROVAL_DECISION,
        CHAT_SESSION_APPROVAL_DECISION, ALWAYS_ALLOW_APPROVAL_DECISION,
        DENY_APPROVAL_DECISION,
    ]
    always = payload["options"][3]
    assert "narrow" in always["description"].lower() or "exactly" in always["description"].lower()
    assert "revocable" in always["description"].lower()


# ── Scoped rules: persistence, matching, scope ────────────────────────────

def _persist(store, *, owner=OWNER, session_id="", tool_name="bash",
             canonical_name="Bash", content="ls -la", workspace,
             session_scoped=None, **overrides):
    caps = _caps(tool_name, content)
    return store.persist(
        owner=owner, session_id=session_id, tool_name=tool_name,
        canonical_name=canonical_name, content=content,
        workspace=workspace,
        effects=tuple(sorted(effect.value for effect in caps.effects)),
        result_integrity=caps.result_integrity.value,
        origin_approval_id="ap-1",
        session_scoped=bool(session_id) if session_scoped is None else session_scoped,
        **overrides,
    )


def test_always_allow_rule_matches_the_exact_operation_across_sessions(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, workspace=str(workspace))
    found = store.find_matching(
        owner=OWNER, session_id="a-different-session", tool_name="Bash",
        content="ls -la", workspace=str(workspace),
        capabilities=_caps("bash", "ls -la"),
    )
    assert found is not None  # persistent rule: any future session of the owner


def test_rule_requires_the_exact_content(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, workspace=str(workspace))
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la /tmp",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la /tmp"),
    ) is None
    # A compound command is a DIFFERENT operation — never covered by one
    # approved component.
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash",
        content="ls -la && rm -rf /", workspace=str(workspace),
        capabilities=_caps("bash", "ls -la && rm -rf /"),
    ) is None


def test_rule_binds_the_effective_cwd(tmp_path):
    store = _ScopedRuleStore()
    workspace_a = tmp_path / "ws-a"
    workspace_a.mkdir()
    _persist(store, workspace=str(workspace_a))
    workspace_b = tmp_path / "ws-b"
    workspace_b.mkdir()
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la",
        workspace=str(workspace_b), capabilities=_caps("bash", "ls -la"),
    ) is None


def test_rule_matches_either_tool_spelling_but_never_another_tool(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, workspace=str(workspace))
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is not None
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="Bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is not None
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="python", content="ls -la",
        workspace=str(workspace), capabilities=_caps("python", "ls -la"),
    ) is None


def test_rule_never_leaks_across_owners(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, workspace=str(workspace))
    assert store.find_matching(
        owner="mallory", session_id=SESSION, tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is None
    assert store.list_rules("mallory") == []


def test_session_scoped_rule_stays_inside_its_session(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, session_id="session-a", workspace=str(workspace))
    assert store.find_matching(
        owner=OWNER, session_id="session-b", tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is None
    assert store.find_matching(
        owner=OWNER, session_id="session-a", tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is not None


def test_expired_session_rule_is_refused(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rule = _persist(store, session_id="session-a", workspace=str(workspace))
    expired = type(rule)(
        **{**rule.__dict__, "expires_at": time.time() - 1},
    )
    store._rules[rule.rule_id] = expired
    assert store.find_matching(
        owner=OWNER, session_id="session-a", tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is None


def test_revoked_rule_is_refused(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rule = _persist(store, workspace=str(workspace))
    assert store.revoke(OWNER, rule.rule_id) is True
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    ) is None
    assert store.revoke("mallory", rule.rule_id) is False  # cross-owner revoke


def test_capability_change_makes_the_rule_inert(tmp_path):
    # A policy/classification change altered the operation's classified
    # capabilities: the saved rule must not approve the new shape.
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _persist(store, workspace=str(workspace))
    saved = store.list_rules(OWNER)[0]
    # Simulate a classifier change that reclassifies the operation with an
    # additional mutating effect: the fingerprint no longer matches.
    from src.tool_capabilities import ResultIntegrity, ToolCapabilities, ToolEffect

    changed_caps = ToolCapabilities(
        effects={ToolEffect.EXECUTE_CODE, ToolEffect.WRITE_WORKSPACE},
        result_integrity=ResultIntegrity.WORKSPACE_UNTRUSTED,
        known=True,
    )
    assert tuple(sorted(effect.value for effect in changed_caps.effects)) != tuple(
        saved["effects"])
    assert store.find_matching(
        owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=changed_caps,
    ) is None


def test_persistent_rules_survive_a_restart(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rule = _persist(store, workspace=str(workspace))
    fresh = _ScopedRuleStore()  # a restarted process loads the same file
    found = fresh.find_matching(
        owner=OWNER, session_id="new-session", tool_name="bash", content="ls -la",
        workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
    )
    assert found is not None and found.rule_id == rule.rule_id


def test_malformed_store_fails_closed(tmp_path):
    import src.tool_approval_rules as rules_module

    path = tmp_path / "approval_rules.json"
    path.write_text("{ not json at all")
    monkey_path = rules_module._rules_path
    rules_module._rules_path = lambda: str(path)
    try:
        store = _ScopedRuleStore()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert store.find_matching(
            owner=OWNER, session_id=SESSION, tool_name="bash", content="ls -la",
            workspace=str(workspace), capabilities=_caps("bash", "ls -la"),
        ) is None
        # Unknown store version is ignored the same way.
        path.write_text(json.dumps({"version": 99, "rules": []}))
        store2 = _ScopedRuleStore()
        assert store2.list_rules(OWNER) == []
    finally:
        rules_module._rules_path = monkey_path


def test_too_many_rules_are_refused_not_silently_dropped(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for index in range(RULES_MAX_PER_OWNER):
        _persist(store, content=f"cmd-{index}", workspace=str(workspace))
    with pytest.raises(ValueError):
        _persist(store, content="one-too-many", workspace=str(workspace))


def test_duplicate_rules_deduplicate_by_operation(tmp_path):
    store = _ScopedRuleStore()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = _persist(store, workspace=str(workspace))
    second = _persist(store, workspace=str(workspace))
    assert first.rule_id != second.rule_id
    assert len(store.list_rules(OWNER)) == 1


# ── Gateway decision boundary (admitted, interactive) ─────────────────────

def _admitted(kind="browser", owner=OWNER):
    from src.gateway_admission import GatewayAdmission
    from src.session_repository import SessionOwnership

    scopes = frozenset({"chat"}) if kind == "bearer" else frozenset()
    return GatewayAdmission().admit(
        Principal(owner, kind, scopes), SESSION,
        lambda _: SessionOwnership(owner),
    )


def test_gateway_consumes_allow_once_and_persists_nothing():
    from src.gateway_approval_decisions import consume_admitted_tool_approval

    store = ToolApprovalStore()
    admission = _admitted()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="ls -la",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "ls -la"),
    )
    resolution = consume_admitted_tool_approval(
        admission, pending.approval_id, ALLOW_ONCE_APPROVAL_DECISION,
        plan_mode=False, internal_tool_request=False, store=store,
    )
    assert resolution.decision == ALLOW_ONCE_APPROVAL_DECISION
    assert resolution.exact.scope is ToolApprovalScope.SINGLE_ACTION
    assert resolution.saved_rule is None
    assert store.peek(pending.approval_id) is None


def test_gateway_always_allow_persists_a_narrow_rule(tmp_path):
    from src.gateway_approval_decisions import consume_admitted_tool_approval

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = ToolApprovalStore()
    admission = _admitted()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="ls -la",
        workspace=str(workspace), external_untrusted_context_seen=True,
        capabilities=_caps("bash", "ls -la"),
    )
    resolution = consume_admitted_tool_approval(
        admission, pending.approval_id, ALWAYS_ALLOW_APPROVAL_DECISION,
        plan_mode=False, internal_tool_request=False, store=store,
    )
    assert resolution.saved_rule is not None
    rule = resolution.saved_rule
    assert rule.tool_name == "bash" and rule.canonical_name == "Bash"
    assert rule.content == "ls -la"
    from src.tool_approval_rules import find_matching_scoped_rule

    # The saved rule now suppresses the prompt for the exact operation in a
    # future session of the same owner — but matches nothing else.
    assert find_matching_scoped_rule(
        owner=OWNER, session_id="future-session", tool_name="Bash",
        content="ls -la", workspace=str(workspace),
        capabilities=_caps("bash", "ls -la"),
    ) is not None
    assert find_matching_scoped_rule(
        owner=OWNER, session_id="future-session", tool_name="Bash",
        content="ls -la /tmp", workspace=str(workspace),
        capabilities=_caps("bash", "ls -la /tmp"),
    ) is None


def test_gateway_rejects_an_unknown_decision_fail_closed():
    from src.gateway_approval_decisions import inspect_admitted_tool_approval

    store = ToolApprovalStore()
    admission = _admitted()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="pwd",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "pwd"),
    )
    with pytest.raises(AdmissionDenied) as excinfo:
        inspect_admitted_tool_approval(
            admission, pending.approval_id, "approve_everything",
            plan_mode=False, internal_tool_request=False, store=store,
        )
    assert "Invalid tool approval decision" in str(excinfo.value.detail)
    # Fail-closed: the pending approval is NOT consumed by the malformed
    # decision and can still be answered properly.
    assert store.peek(pending.approval_id) is not None


def test_gateway_deny_consumes_the_prompt_and_grants_nothing():
    from src.gateway_approval_decisions import consume_admitted_tool_approval

    store = ToolApprovalStore()
    admission = _admitted()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="bash", content="rm -rf old",
        workspace="/tmp/x", external_untrusted_context_seen=True,
        capabilities=_caps("bash", "rm -rf old"),
    )
    resolution = consume_admitted_tool_approval(
        admission, pending.approval_id, DENY_APPROVAL_DECISION,
        plan_mode=False, internal_tool_request=False, store=store,
    )
    assert resolution.exact is None and resolution.saved_rule is None
    assert store.peek(pending.approval_id) is None
