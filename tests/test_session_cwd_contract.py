"""M5.3 increment 1: natural CWD and persistent session directories.

The contract (src/session_cwd.py is the single documented resolution point):

* Local CLI/TUI sessions start in the invoking shell's pwd; Web/Telegram and
  remote-default sessions start in the execution account's HOME; the
  filesystem root is never selected and a per-conversation workspace is never
  mandatory.
* A selected CWD is validated SERVER-SIDE, persisted on the shared session
  row, returned to the client, and shared by every authorized client of the
  session. An invalid selection is refused with an actionable reason and
  keeps the previous value.
* CWD is not the sandbox: binding a directory changes where relative paths
  resolve and subprocesses run — every access still passes the existing
  authority, vetting and resource confinement. An approval sealed for one
  workspace cannot execute against another.
"""
import os
import types
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
from routes import session_routes as sr
from src.session_cwd import (
    clearable_session_cwd,
    default_session_cwd_for_surface,
    execution_home,
    vetted_session_cwd,
)


# ── Resolution contract unit tests ────────────────────────────────────────

def test_execution_home_is_a_real_directory():
    home = execution_home()
    assert home and os.path.isdir(home)
    assert os.path.dirname(home) != home  # never a filesystem root


def test_web_and_remote_defaults_are_the_execution_home():
    for surface in ("web", "telegram", None):
        canonical, reason = default_session_cwd_for_surface(surface)
        assert canonical == execution_home(), (surface, reason)
        assert reason is None


def test_local_cli_and_tui_start_in_the_invoking_pwd(tmp_path):
    pwd = tmp_path / "project"
    pwd.mkdir()
    canonical, reason = default_session_cwd_for_surface(
        "odysseus-tui", invoking_cwd=str(pwd)
    )
    assert canonical == os.path.realpath(pwd)
    assert reason is None
    canonical, _ = default_session_cwd_for_surface("cli", invoking_cwd=str(pwd))
    assert canonical == os.path.realpath(pwd)


def test_unusable_invoking_pwd_falls_back_to_home(tmp_path):
    deleted = tmp_path / "deleted-dir"
    canonical, reason = default_session_cwd_for_surface(
        "odysseus-tui", invoking_cwd=str(deleted)
    )
    assert canonical == execution_home()
    assert reason is None
    # No implicit substitution by a repository root or generated workspace.
    assert "repo" not in canonical and ".mnerys" not in canonical


def test_remote_never_inherits_the_client_pwd():
    client_pwd = "/home/some-remote-user/work"
    canonical, _ = default_session_cwd_for_surface(
        "odysseus-tui", invoking_cwd=client_pwd, remote=True,
    )
    assert canonical == execution_home()
    assert canonical != client_pwd


def test_filesystem_root_is_never_selected():
    canonical, reason = vetted_session_cwd("/")
    assert canonical is None
    assert "root" in reason.lower()


def test_nonexistent_directory_is_refused_with_a_clear_reason(tmp_path):
    canonical, reason = vetted_session_cwd(str(tmp_path / "nope"))
    assert canonical is None
    assert "does not exist" in reason.lower()


def test_sensitive_directory_is_refused(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    canonical, reason = vetted_session_cwd(str(ssh_dir))
    assert canonical is None
    assert "sensitive" in reason.lower() or "restricted" in reason.lower()


def test_symlinked_directory_resolves_to_its_target(tmp_path):
    real = tmp_path / "real-dir"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    canonical, _ = vetted_session_cwd(str(link))
    assert canonical == os.path.realpath(real)
    # A swap to a sensitive target is caught at the next validation.
    victim = tmp_path / ".ssh"
    victim.mkdir()
    os.unlink(link)
    os.symlink(victim, link)
    canonical, reason = vetted_session_cwd(str(link))
    assert canonical is None


def test_empty_value_clears_and_keeps_the_previous_value_safe(tmp_path):
    value, reason = clearable_session_cwd("   ")
    assert value is None and reason is None
    canonical, _ = clearable_session_cwd(str(tmp_path))
    assert canonical == os.path.realpath(tmp_path)


def test_control_characters_and_length_are_refused():
    canonical, reason = vetted_session_cwd("/tmp/x\nrm -rf /")
    assert canonical is None and "control characters" in reason.lower()
    canonical, reason = vetted_session_cwd("/tmp/" + "a" * 5000)
    assert canonical is None and "too long" in reason.lower()


# ── Session API: validated, persisted selection ───────────────────────────

_APP_STATE = {"router": None, "manager": None}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """One router built once (the module-level APIRouter accumulates route
    functions per setup call); per-test state resets underneath it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(sr, "SessionLocal", factory)

    def _row(session_id):
        db = sr.SessionLocal()
        try:
            return db.query(DbSession).filter(DbSession.id == session_id).first()
        finally:
            db.close()

    class _Manager:
        def __init__(self):
            self.sessions = {}

        def reset(self):
            self.sessions.clear()

        def _db(self):
            # Always resolve through the patched module attribute so every
            # test's disposable database is the one actually used.
            return sr.SessionLocal()

        def create_session(self, session_id, name, endpoint_url, model, **kwargs):
            db = self._db()
            try:
                db.add(DbSession(
                    id=session_id, name=name, endpoint_url=endpoint_url,
                    model=model, owner=kwargs.get("owner"),
                    cwd=kwargs.get("cwd"), rag=kwargs.get("rag", False),
                    revision=0, incarnation_id=uuid4().hex,
                    created_at=utcnow_naive(), updated_at=utcnow_naive(),
                ))
                db.commit()
            finally:
                db.close()
            session = SimpleNamespace(
                id=session_id, name=name, endpoint_url=endpoint_url, model=model,
                owner=kwargs.get("owner"), cwd=kwargs.get("cwd"), rag=False,
                archived=False, headers={}, endpoint_id=None,
                incarnation_id=uuid4().hex, incognito=False, thinking_mode="off",
            )
            self.sessions[session_id] = session
            return session

        def get_session(self, session_id):
            if session_id not in self.sessions:
                raise KeyError(session_id)
            return self.sessions[session_id]

        def update_session_name(self, session_id, name):
            self.sessions[session_id].name = name

    manager = _Manager()

    def _owner_ok(request, sid):
        row = _row(sid)
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(404, f"Session {sid} not found")
        return SimpleNamespace(session_id=sid, owner=row.owner)

    monkeypatch.setattr(sr, "_verify_session_owner", lambda request, sid: _owner_ok(request, sid))
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda user: True,
    )
    monkeypatch.setattr(
        "src.auth_helpers._auth_disabled", lambda: True, raising=False,
    )
    monkeypatch.setattr(
        sr, "is_delegated_credential", lambda request: False, raising=False,
    )
    monkeypatch.setattr(sr, "_current_user_is_admin", lambda request, user: True, raising=False)
    monkeypatch.setattr(
        "src.auth_helpers.require_chat_api_token_scope", lambda *a, **k: None,
        raising=False,
    )

    if _APP_STATE["router"] is None:
        _APP_STATE["router"] = sr.setup_session_routes(
            manager, {"REQUEST_TIMEOUT": 20}, webhook_manager=None,
        )
        _APP_STATE["manager"] = manager
        app = FastAPI()
        app.include_router(_APP_STATE["router"])
        _APP_STATE["client"] = TestClient(app)
    else:
        _APP_STATE["manager"].sessions = manager.sessions
    _APP_STATE["manager"].sessions = manager.sessions
    yield _APP_STATE["client"], _row
    manager.reset()
    _APP_STATE["manager"].sessions = manager.sessions


def test_create_defaults_to_the_execution_home_when_allowed(client, monkeypatch):
    tc, _row = client
    response = tc.post("/api/session", data={"name": "home session", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cwd"] == execution_home()


def test_create_rejects_an_invalid_directory_with_a_clear_reason(client):
    tc, _row = client
    response = tc.post("/api/session", data={"name": "bad", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true", "cwd": "/"})
    assert response.status_code == 400
    assert "root" in response.json()["detail"].lower()


def test_create_non_admin_gets_no_binding_even_with_a_directory(client, monkeypatch):
    tc, _row = client
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: False)
    response = tc.post("/api/session", data={"name": "limited", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true", "cwd": "/tmp"})
    assert response.status_code == 200
    assert response.json()["cwd"] in (None, "")


def test_patch_selects_and_persists_the_directory(client, monkeypatch, tmp_path):
    tc, _row = client
    created = tc.post("/api/session", data={"name": "s", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true"}).json()
    target = tmp_path / "workspace-dir"
    target.mkdir()
    response = tc.patch(f"/api/session/{created['id']}", data={"cwd": str(target)})
    assert response.status_code == 200, response.text
    assert response.json()["cwd"] == os.path.realpath(target)
    assert _row(created["id"]).cwd == os.path.realpath(target)


def test_patch_refuses_an_invalid_directory_and_keeps_the_previous_one(client, monkeypatch, tmp_path):
    tc, _row = client
    created = tc.post("/api/session", data={"name": "s", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true"}).json()
    previous = created["cwd"]
    response = tc.patch(f"/api/session/{created['id']}", data={"cwd": "/definitely/not/here"})
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"].lower()
    assert _row(created["id"]).cwd == previous


def test_patch_empty_value_clears_the_selection(client, monkeypatch):
    tc, _row = client
    created = tc.post("/api/session", data={"name": "s", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true"}).json()
    response = tc.patch(f"/api/session/{created['id']}", data={"cwd": "   "})
    assert response.status_code == 200
    assert response.json()["cwd"] in (None, "")
    assert _row(created["id"]).cwd in (None, "")


def test_cross_owner_cwd_change_is_denied(client, monkeypatch, tmp_path):
    tc, _row = client

    def deny(request, sid):
        row = _row(sid)
        from fastapi import HTTPException
        if row is None or row.owner != "mallory":
            raise HTTPException(403, "Not your session")

    created = tc.post("/api/session", data={"name": "s", "endpoint_url": "http://localhost/v1/chat/completions", "model": "m",
        "skip_validation": "true"}).json()
    monkeypatch.setattr(sr, "_verify_session_owner", deny)
    target = tmp_path / "mallory-dir"
    target.mkdir()
    response = tc.patch(f"/api/session/{created['id']}", data={"cwd": str(target)})
    assert response.status_code == 403
    assert _row(created["id"]).cwd != os.path.realpath(target)


# ── Approval binding: a stale CWD cannot redirect execution ───────────────

def test_approval_sealed_for_one_workspace_cannot_execute_against_another(tmp_path):
    from src.tool_approvals import TASK_APPROVAL_DECISION, ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    workspace_a = tmp_path / "ws-a"
    workspace_a.mkdir()
    pending = store.create(
        owner="alice", session_id="sess-1", origin_run_id="run-1",
        tool_name="edit_file",
        content="note.md\nold -> new",
        workspace=str(workspace_a),
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("edit_file", "note.md\nold -> new"),
    )
    assert pending is not None
    workspace_b = tmp_path / "ws-b"
    workspace_b.mkdir()
    # The operator approves through the supported mechanism; the consumed
    # record is the exact-operation identity the loop replays.
    approval = store.consume(
        pending.approval_id,
        decision=TASK_APPROVAL_DECISION,
        owner="alice",
        session_id="sess-1",
        allow_continuation=True,
    )
    assert approval is not None
    # The sealed operation still matches its own workspace...
    assert approval.matches(
        owner="alice", session_id="sess-1", tool_name="edit_file",
        content="note.md\nold -> new", workspace=str(workspace_a),
    )
    # ...and NOT the directory the session switched to afterwards: a stale
    # approval cannot redirect execution to another CWD.
    assert not approval.matches(
        owner="alice", session_id="sess-1", tool_name="edit_file",
        content="note.md\nold -> new", workspace=str(workspace_b),
    )
    # The sealed replay is one-use: the first claim succeeds, a replay of
    # the same operation identity is refused.
    assert approval.claim(
        owner="alice", session_id="sess-1", tool_name="edit_file",
        content="note.md\nold -> new", workspace=str(workspace_a),
    ) is True
    assert approval.claim(
        owner="alice", session_id="sess-1", tool_name="edit_file",
        content="note.md\nold -> new", workspace=str(workspace_a),
    ) is False


# ── Execution: tools honor the selected directory ─────────────────────────

def test_relative_paths_resolve_against_the_selected_cwd(tmp_path, monkeypatch):
    import asyncio
    import json as jsonlib

    from src.tool_execution import _active_workspace
    from src.agent_tools.filesystem_tools import WriteFileTool, ReadFileTool

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    token = _active_workspace.set(str(selected))
    try:
        ctx = {"owner": "alice", "session_id": "s"}
        # The write_file handler's content contract is the line-form
        # encoding (path on the first line, content after) — identical to a
        # native call after registry conversion.
        written = asyncio.run(WriteFileTool().execute(
            "relative-note.md\nrelative body", ctx))
        assert written["exit_code"] == 0, written
        assert (selected / "relative-note.md").read_text(encoding="utf-8") == "relative body"
        assert not (outside / "relative-note.md").exists()
        read = asyncio.run(ReadFileTool().execute(
            "relative-note.md", ctx))
        assert "relative body" in jsonlib.dumps(read)
    finally:
        _active_workspace.reset(token)


def test_subprocess_and_search_roots_follow_the_selected_directory(tmp_path, monkeypatch):
    # The resolution contract for foreground/background subprocesses
    # (agent_cwd) and search tools (_resolve_search_root): the selected
    # session directory wins over the default agent workspace.
    from src.tool_execution import (
        _active_workspace,
        _resolve_search_root,
        agent_cwd,
        default_agent_workspace,
    )

    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "marker.txt").write_text("x", encoding="utf-8")
    assert agent_cwd() == default_agent_workspace()  # no binding: default
    token = _active_workspace.set(str(selected))
    try:
        assert agent_cwd() == os.path.realpath(selected)
        assert _resolve_search_root("") == os.path.realpath(selected)
    finally:
        _active_workspace.reset(token)


def test_model_visible_context_describes_the_selected_directory(tmp_path, monkeypatch):
    from routes.chat_routes import _client_runtime_context_system_message

    selected = tmp_path / "selected"
    selected.mkdir()
    message = _client_runtime_context_system_message(
        {"surface": "odysseus-tui", "session_cwd": str(selected)})
    assert message is not None
    assert str(selected) in str(message.get("content") or "")


def test_deleted_selected_directory_is_reported_not_silently_executed(tmp_path, monkeypatch):
    # The per-turn binding re-vets the persisted CWD: a directory that
    # disappeared yields an actionable failure, not silent execution
    # somewhere else.
    import asyncio
    import json as jsonlib

    from src.agent_tools.subprocess_tools import BashTool
    from src.tool_execution import _active_workspace

    from src.tool_execution import _active_workspace, agent_cwd

    selected = tmp_path / "vanishing"
    selected.mkdir()
    token = _active_workspace.set(str(selected))
    try:
        selected.rmdir()  # the selected directory disappears
        with pytest.raises(RuntimeError) as excinfo:
            agent_cwd()
        # The contract refuses to redirect execution: the error names the
        # directory problem instead of running somewhere else.
        assert "safe real directory" in str(excinfo.value)
    finally:
        _active_workspace.reset(token)


# ── Gateway v1 surface: shared session CWD contract ───────────────────────

def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v1-sessions.db'}")
    core_database.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_v1_endpoint(tmp_path):
    """One registered, enabled endpoint row for gateway v1 session tests."""
    from core.database import ModelEndpoint

    db = core_database.SessionLocal()
    try:
        row = ModelEndpoint(
            id="ep-1", name="local", base_url="http://localhost/v1",
            api_key="", is_enabled=True,
        )
        if hasattr(ModelEndpoint, "owner"):
            row.owner = "alice"
        db.add(row)
        db.commit()
    finally:
        db.close()


from tests.test_mnerys_gateway_sessions_v1 import FakeManager  # noqa: E402


class _CwdFakeManager(FakeManager):
    """FakeManager plus the cwd kwarg create_owner_session now passes."""

    def create_session(self, *, session_id: str, name: str, endpoint_url: str,
                       model: str, owner: str, endpoint_id=None, headers=None,
                       cwd=None):
        session = super().create_session(
            session_id=session_id, name=name, endpoint_url=endpoint_url,
            model=model, owner=owner, endpoint_id=endpoint_id, headers=headers,
        )
        session.cwd = cwd
        db = core_database.SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is not None:
                row.cwd = cwd
                db.commit()
        finally:
            db.close()
        return session


def test_gateway_v1_create_validates_and_persists_cwd(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.gateway_principal
    from routes import gateway_api
    from tests.test_mnerys_gateway_sessions_v1 import FakeManager

    target = tmp_path / "v1-workdir"
    target.mkdir()
    # Patch the disposable database FIRST — never touch the production store.
    monkeypatch.setattr(core_database, "SessionLocal", _session_factory(tmp_path))
    _seed_v1_endpoint(tmp_path)
    manager = _CwdFakeManager()
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(
        gateway_api, "_session_manager", lambda request: manager, raising=False,
    )
    monkeypatch.setattr(
        gateway_api, "_require_owner", lambda request: "alice", raising=False,
    )
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda user: True,
    )
    monkeypatch.setattr(
        "src.llm_core.list_model_ids", lambda *a, **k: ["m"], raising=False,
    )
    tc = TestClient(app)
    # A validated explicit selection persists.
    created = tc.post(
        "/api/v1/sessions",
        json={"name": "v1 session", "endpoint_id": "ep-1", "cwd": str(target)},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["cwd"] == os.path.realpath(target)
    # An invalid selection is refused with the actionable reason.
    refused = tc.post(
        "/api/v1/sessions",
        json={"name": "bad", "endpoint_id": "ep-1", "cwd": "/"},
    )
    assert refused.status_code == 400
    assert "root" in refused.json()["detail"].lower()
    # The default (no cwd) is the execution HOME.
    default = tc.post("/api/v1/sessions", json={"name": "default", "endpoint_id": "ep-1"})
    assert default.status_code == 201
    assert default.json()["cwd"] == execution_home()


def test_gateway_v1_update_and_detail_round_trip_cwd(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.gateway_principal
    from routes import gateway_api
    from src.gateway_admission import Principal
    from tests.test_mnerys_gateway_sessions_v1 import FakeManager

    target = tmp_path / "selected-dir"
    target.mkdir()
    manager = _CwdFakeManager()
    monkeypatch.setattr(core_database, "SessionLocal", _session_factory(tmp_path))
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(
        gateway_api, "_session_manager", lambda request: manager, raising=False,
    )
    monkeypatch.setattr(
        gateway_api, "_require_owner", lambda request: "alice", raising=False,
    )
    _fake_principal = lambda request: Principal("alice", "browser", frozenset({"chat"}))
    monkeypatch.setattr(
        routes.gateway_principal, "principal_for_chat_request", _fake_principal,
    )
    monkeypatch.setattr(
        gateway_api, "principal_for_chat_request", _fake_principal,
    )
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda user: True,
    )
    monkeypatch.setattr(
        "src.llm_core.list_model_ids", lambda *a, **k: ["m"], raising=False,
    )
    tc = TestClient(app)
    sid = f"v1-{uuid4().hex[:8]}"
    manager.create_session(
        session_id=sid, name="s", endpoint_url="http://localhost/v1/chat/completions",
        model="m", owner="alice",
    )
    updated = tc.patch(f"/api/v1/sessions/{sid}", json={"cwd": str(target)})
    assert updated.status_code == 200, updated.text
    assert updated.json()["cwd"] == os.path.realpath(target)
    detail = tc.get(f"/api/v1/sessions/{sid}")
    assert detail.status_code == 200
    assert detail.json()["session"]["cwd"] == os.path.realpath(target)
    cleared = tc.patch(f"/api/v1/sessions/{sid}", json={"cwd": ""})
    assert cleared.status_code == 200
    assert cleared.json()["cwd"] in (None, "")
