"""M5.3 increment 4: secure opt-in YOLO mode.

YOLO is server-owned, session-scoped, explicitly enabled (confirmed) state
that suppresses only DISCRETIONARY approval prompts: the risk classifier's
REQUIRES_APPROVAL and the external-untrusted-context gate. Delegated
boundaries, tool-policy denials, DENIED risk classifications, request
authority, filesystem confinement, read-before-edit, artifact publication,
auditing, cancellation and replay protection remain fully enforced. Model
text, skills, MCP content and history can never toggle it.
"""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as core_database
import core.database as cdb
from core.database import Session as DbSession, utcnow_naive
from src.tool_capabilities import ToolRunSecurityContext

OWNER = "alice"
SESSION = "yolo-session"


@pytest.fixture()
def sec():
    return ToolRunSecurityContext()


@pytest.fixture()
def _yolo_off(sec):
    assert sec.yolo_enabled is False  # the default
    yield sec


# ── Gate behavior ─────────────────────────────────────────────────────────

def test_yolo_default_off_prompts_for_risky_commands(_yolo_off):
    sec = _yolo_off
    assert sec.decision_for("bash", "echo harmless").allowed
    decision = sec.decision_for("bash", "rm -rf build/")
    assert not decision.allowed
    assert "approval" in decision.reason.lower()


def test_yolo_enabled_suppresses_the_risk_prompt_but_still_classifies(sec, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="src.tool_capabilities")
    sec.yolo_enabled = True
    assert sec.decision_for("bash", "rm -rf build/").allowed
    assert sec.decision_for("bash", "git clean -fd").allowed
    # The classification was still performed and audited.
    assert any("YOLO suppressed risk approval" in message for message in caplog.messages)


def test_yolo_never_lifts_a_denied_or_delegated_boundary():
    # Delegated credentials: workspace reads are refused outright, YOLO or not.
    delegated = ToolRunSecurityContext(delegated_credential=True, yolo_enabled=True)
    assert not delegated.decision_for("read_file", "notes.md").allowed
    # The classifier's DENIED category is non-delegable (no approval lifts it).
    from src.command_risk import CommandRiskClassification, DENIED

    sec = ToolRunSecurityContext(yolo_enabled=True)
    import src.command_risk as risk_module
    original = risk_module.classify_shell_command
    risk_module.classify_shell_command = lambda command: CommandRiskClassification(
        DENIED, "critical", "Prohibited operation (test stub).",
    )
    try:
        import src.tool_capabilities as caps_module
        caps_module.classify_shell_command = risk_module.classify_shell_command
        decision = sec.decision_for("bash", "rm -rf /")
        assert not decision.allowed
        assert "Prohibited" in decision.reason
    finally:
        risk_module.classify_shell_command = original
        caps_module.classify_shell_command = original


def test_yolo_suppresses_the_external_context_gate_for_known_tools(sec):
    sec.yolo_enabled = True
    sec.observe_messages([{
        "role": "user",
        "metadata": {"trusted": False, "tool_gate_untrusted": True, "source": "web page"},
    }])
    assert sec.external_untrusted_context_seen is True
    # A understood tool's discretionary prompt is suppressed.
    assert sec.decision_for("bash", "echo harmless").allowed
    assert sec.decision_for("write_file", "note.md\nbody").allowed
    # Unknown capabilities stay refused (fail closed).
    import json as jsonlib
    unknown = ToolRunSecurityContext(yolo_enabled=True)
    unknown.observe_messages([{
        "role": "user",
        "metadata": {"trusted": False, "tool_gate_untrusted": True, "source": "web page"},
    }])
    decision = unknown.decision_for("mcp__unknown__uninspected", "{}")
    assert not decision.allowed


def test_yolo_leaves_saved_rules_and_ordinary_flow_unchanged(tmp_path):
    import src.tool_approval_rules as rules_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkey_rules = rules_module._rules_path
    rules_module._rules_path = lambda: str(tmp_path / "rules.json")
    reset = rules_module.bind_scoped_approval_rules(_ScopedRuleStoreForTests())
    try:
        from src.tool_capabilities import capabilities_for_action

        caps = capabilities_for_action("bash", "rm -rf build/")
        rules_module.persist_scoped_rule(
            owner=OWNER, session_id="", tool_name="bash", canonical_name="Bash",
            content="rm -rf build/", workspace=str(workspace),
            effects=tuple(sorted(effect.value for effect in caps.effects)),
            result_integrity=caps.result_integrity.value,
            origin_approval_id="ap-y", session_scoped=False,
        )
        # With YOLO OFF the rule still suppresses the exact reviewed prompt.
        sec = ToolRunSecurityContext(yolo_enabled=False)
        assert not sec.decision_for("bash", "rm -rf build/").allowed
        found = rules_module.find_matching_scoped_rule(
            owner=OWNER, session_id="any-session", tool_name="bash",
            content="rm -rf build/", workspace=str(workspace), capabilities=caps,
        )
        assert found is not None
    finally:
        rules_module._rules_path = monkey_rules
        reset()


def _ScopedRuleStoreForTests():
    from src.tool_approval_rules import _ScopedRuleStore

    return _ScopedRuleStore()


# ── Session state: the shared Gateway contract ────────────────────────────

@pytest.fixture()
def session_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'yolo.db'}")
    core_database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(core_database, "SessionLocal", factory)
    monkeypatch.setattr(cdb, "SessionLocal", factory)

    def _row(session_id):
        db = factory()
        try:
            return db.query(DbSession).filter(DbSession.id == session_id).first()
        finally:
            db.close()

    yield factory, _row


def _row_factory(factory, **kwargs):
    db = factory()
    try:
        row = DbSession(
            id=kwargs.get("id") or f"s-{uuid4().hex[:8]}",
            name=kwargs.get("name") or "s",
            endpoint_url="http://localhost/v1/chat/completions",
            model="m", owner=kwargs.get("owner") or OWNER,
            revision=kwargs.get("revision", 0),
            incarnation_id=uuid4().hex,
            created_at=utcnow_naive(), updated_at=utcnow_naive(),
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_migrate_adds_the_yolo_column_defaulting_off(session_env):
    factory, _row = session_env
    sid = _row_factory(factory)
    row = None
    db = factory()
    try:
        row = db.query(DbSession).filter(DbSession.id == sid).first()
    finally:
        db.close()
    assert row.yolo_enabled is False  # new sessions never inherit YOLO


def test_v1_yolo_toggle_enable_requires_confirmation(tmp_path, monkeypatch, session_env):
    from routes import gateway_api
    from tests.test_mnerys_gateway_sessions_v1 import FakeManager

    factory, _row = session_env
    sid = _row_factory(factory)
    manager = FakeManager()
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(gateway_api, "_session_manager", lambda request: manager, raising=False)
    monkeypatch.setattr(gateway_api, "_require_owner", lambda request: OWNER, raising=False)
    import routes.gateway_principal
    from src.gateway_admission import Principal

    fake_principal = lambda request: Principal(OWNER, "browser", frozenset({"chat"}))
    monkeypatch.setattr(routes.gateway_principal, "principal_for_chat_request", fake_principal)
    monkeypatch.setattr(gateway_api, "principal_for_chat_request", fake_principal)
    monkeypatch.setattr(
        "src.auth_helpers._auth_disabled", lambda: True, raising=False,
    )
    monkeypatch.setattr(gateway_api, "_current_user_is_admin", lambda request, user: True, raising=False)
    tc = TestClient(app)

    # Unconfirmed enablement is refused — never a silent activation.
    refused = tc.post(f"/api/v1/sessions/{sid}/yolo", json={"enabled": True})
    assert refused.status_code == 400
    assert "confirmation" in refused.json()["detail"].lower()
    confirmed = tc.post(
        f"/api/v1/sessions/{sid}/yolo",
        json={"enabled": True, "confirmed": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["yolo_enabled"] is True
    assert _row(sid).yolo_enabled is True
    # Disabling needs no confirmation and is always allowed.
    disabled = tc.post(f"/api/v1/sessions/{sid}/yolo", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["yolo_enabled"] is False
    # Malformed body fails closed.
    malformed = tc.post(f"/api/v1/sessions/{sid}/yolo", json={"enabled": "yes"})
    assert malformed.status_code == 400


def test_v1_yolo_state_persists_across_restart_and_is_isolated(session_env, tmp_path, monkeypatch):
    from routes import gateway_api
    from tests.test_mnerys_gateway_sessions_v1 import FakeManager

    factory, _row = session_env
    sid_a = _row_factory(factory)
    sid_b = _row_factory(factory)
    manager = FakeManager()
    # Register the sessions in the manager so the detail route reads the SAME
    # objects the toggle updates (mirroring the real session manager).
    for registered_sid in (sid_a, sid_b):
        manager.sessions[registered_sid] = SimpleNamespace(
            id=registered_sid, name="s", model="m", owner=OWNER,
            yolo_enabled=False, revision=0, incarnation_id=uuid4().hex,
            headers={}, cwd=None,
        )
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(gateway_api, "_session_manager", lambda request: manager, raising=False)
    monkeypatch.setattr(gateway_api, "_require_owner", lambda request: OWNER, raising=False)
    import routes.gateway_principal
    from src.gateway_admission import Principal

    fake_principal = lambda request: Principal(OWNER, "browser", frozenset({"chat"}))
    monkeypatch.setattr(routes.gateway_principal, "principal_for_chat_request", fake_principal)
    monkeypatch.setattr(gateway_api, "principal_for_chat_request", fake_principal)
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True, raising=False)
    monkeypatch.setattr(gateway_api, "_current_user_is_admin", lambda request, user: True, raising=False)
    tc = TestClient(app)

    enabled = tc.post(
        f"/api/v1/sessions/{sid_a}/yolo", json={"enabled": True, "confirmed": True},
    )
    assert enabled.status_code == 200
    revision = enabled.json()["revision"]
    # The unrelated session stays OFF.
    assert _row(sid_b).yolo_enabled is False
    # A stale concurrent toggle (old revision) is refused deterministically.
    stale = tc.post(
        f"/api/v1/sessions/{sid_a}/yolo",
        json={"enabled": False, "expected_revision": max(0, revision - 1)},
    )
    assert stale.status_code == 409
    # Detail reflects the effective server state (client sync after reconnect).
    detail = tc.get(f"/api/v1/sessions/{sid_a}")
    assert detail.json()["session"]["yolo_enabled"] is True
    # "Restart" durability: the row is the source of truth — a fresh process
    # reloads the persisted state (the row survives; the manager is in-memory).
    assert _row(sid_a).yolo_enabled is True


def test_cross_owner_yolo_toggle_is_refused(session_env, tmp_path, monkeypatch):
    from fastapi import HTTPException

    from routes import gateway_api

    factory, _row = session_env
    sid = _row_factory(factory, owner="bob")
    monkeypatch.setattr(gateway_api, "_require_owner", lambda request: OWNER, raising=False)


    def _lookup(looked_sid):
        if looked_sid != sid:
            return None
        return SessionOwnership("bob")

    original = gateway_api._admit

    def _deny_admit(request, looked_sid):
        raise HTTPException(403, "Not your session")

    monkeypatch.setattr(gateway_api, "_admit", _deny_admit, raising=False)
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(
        gateway_api, "_session_manager", lambda request: SimpleNamespace(), raising=False,
    )
    tc = TestClient(app)
    response = tc.post(
        f"/api/v1/sessions/{sid}/yolo", json={"enabled": True, "confirmed": True},
    )
    assert response.status_code == 403
    assert _row(sid).yolo_enabled is False


def test_legacy_patch_yolo_requires_confirmation(tmp_path, monkeypatch):
    from routes import session_routes as sr

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    core_database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(sr, "SessionLocal", factory)
    sid = _row_factory(factory)
    rows_seen = {}

    def _row(session_id):
        db = factory()
        try:
            return db.query(DbSession).filter(DbSession.id == session_id).first()
        finally:
            db.close()

    class _Manager:
        def __init__(self):
            self.sessions = {}

        def create_session(self, **kwargs):
            raise NotImplementedError

        def get_session(self, session_id):
            if session_id not in rows_seen:
                row = _row(session_id)
                if row is None:
                    raise KeyError(session_id)
                rows_seen[session_id] = SimpleNamespace(
                    id=session_id, name=row.name, model=row.model,
                    cwd=row.cwd, yolo_enabled=row.yolo_enabled,
                    incarnation_id=row.incarnation_id, headers={},
                    thinking_mode="off", rag=False, archived=False,
                    endpoint_id=None, incognito=False,
                )
            return rows_seen[session_id]

        def update_session_name(self, session_id, name):
            self.sessions[session_id] = self.get_session(session_id)

    monkeypatch.setattr(sr, "effective_user", lambda request: OWNER)
    monkeypatch.setattr(sr, "_verify_session_owner", lambda request, looked_sid: None)
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: True)
    monkeypatch.setattr(
        "src.auth_helpers.require_chat_api_token_scope", lambda *a, **k: None, raising=False,
    )
    router = sr.setup_session_routes(_Manager(), {"REQUEST_TIMEOUT": 20}, webhook_manager=None)
    app = FastAPI()
    app.include_router(router)
    tc = TestClient(app)
    unconfirmed = tc.patch(f"/api/session/{sid}", data={"yolo": "true"})
    assert unconfirmed.status_code == 400
    assert _row(sid).yolo_enabled is False
    confirmed = tc.patch(f"/api/session/{sid}", data={"yolo": "true", "yolo_confirm": "true"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["yolo_enabled"] is True
    assert _row(sid).yolo_enabled is True
    disabled = tc.patch(f"/api/session/{sid}", data={"yolo": "false"})
    assert disabled.status_code == 200
    assert _row(sid).yolo_enabled is False


# ── Execution and downstream protections ──────────────────────────────────

def test_yolo_does_not_bypass_read_before_edit(tmp_path, monkeypatch):
    """A stale/missing baseline still refuses a write with YOLO enabled: the
    protection lives in the handler, downstream of the approval gate."""
    import asyncio

    from src.agent_tools.filesystem_tools import WriteFileTool
    from src.tool_execution import _active_workspace

    selected = tmp_path / "ws"
    selected.mkdir()
    target = selected / "protected.md"
    target.write_text("real content", encoding="utf-8")
    token = _active_workspace.set(str(selected))
    try:
        ctx = {"owner": OWNER, "session_id": SESSION}
        # No recorded read: the write is refused regardless of YOLO state.
        result = asyncio.run(WriteFileTool().execute("protected.md\nnew content", ctx))
        assert result["exit_code"] == 1
        assert "No recorded read" in str(result.get("error", ""))
    finally:
        _active_workspace.reset(token)


def test_yolo_suppressed_operation_still_records_real_execution(tmp_path, monkeypatch, caplog):
    """The gate allows the operation, the dispatcher still runs it, and the
    audit trail keeps the truthful YOLO suppression record."""
    import asyncio
    import logging

    from src.tool_capabilities import capabilities_for_action

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sec = ToolRunSecurityContext(yolo_enabled=True)
    with caplog.at_level(logging.INFO):
        decision = sec.decision_for("bash", "rm -rf build/")
    assert decision.allowed
    assert any(
        "YOLO suppressed risk approval" in message
        and "rm -rf build" not in message  # no secret/flag content in the audit line
        for message in caplog.messages
    )
    caps = capabilities_for_action("bash", "rm -rf build/")
    assert caps.known  # classification still performed


def test_stream_agent_loop_threads_the_session_yolo_state():
    """The loop reads YOLO from the ADMITTED SESSION — caller options and model
    text can never set it."""
    import inspect

    from src.gateway_agent_execution import _PROTECTED_AGENT_FIELDS, stream_admitted_agent_turn

    assert "yolo_enabled" in _PROTECTED_AGENT_FIELDS
    source = inspect.getsource(stream_admitted_agent_turn)
    assert 'getattr(session, "yolo_enabled", False)' in source
    # And the loop's run security consumes it server-side.
    from src.agent_loop import stream_agent_loop

    loop_source = inspect.getsource(stream_agent_loop)
    assert "yolo_enabled=bool(yolo_enabled)" in loop_source
