"""Real interpreter-restart acceptance for non-executable notes_v1 recovery.

Only file-backed disposable SQLite and explicitly isolated PostgreSQL 17 are
eligible. Each stage runs in a fresh Python process with no model/provider,
application startup, original pending approval store or tool dispatcher.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal
from src.gateway_notes_decision_fence import NotesDecisionFence
from src.gateway_notes_event_journal import (
    NotesEventJournal, notes_event_journal_metadata,
)
from src.session_repository import SessionOwnership
from tests.test_mnerys_gateway_notes_decision_fence import engine as isolated_fence_engine

_OWNER = "alice"
_SESSION = "session-1"
_RECEIPT = "original-user-001"
_APPROVAL = "A" * 43
_DIGEST = "a" * 64
_NOW = time.time()


def _trusted_test_database():
    """Never use a default engine, configured application DB or production URI."""
    if os.environ.get("MNERYS_NOTES_RESTART_ISOLATED") != "1":
        raise RuntimeError("Isolated recovery test opt-in required")
    raw = os.environ["MNERYS_NOTES_RESTART_DSN"]
    url = make_url(raw)
    if url.get_backend_name() == "sqlite":
        root = Path(os.environ["MNERYS_NOTES_RESTART_ROOT"]).resolve(strict=True)
        database = Path(url.database or "").resolve(strict=True)
        if not (database.is_relative_to(root) and database.is_file()):
            raise RuntimeError("Refusing a non-disposable SQLite test database")
    elif not (
        url.get_backend_name() == "postgresql"
        and url.host == "127.0.0.1"
        and url.username == "mnerys_m21_test"
        and url.database == "mnerys_m21_isolated"
        and os.environ.get("MNERYS_M21_PG_TEST_ISOLATED") == "1"
    ):
        raise RuntimeError("Refusing a non-isolated PostgreSQL test database")
    from sqlalchemy import create_engine
    return create_engine(url, pool_pre_ping=True)


def _identity():
    return dict(
        owner=_OWNER, session_id=_SESSION, approval_id=_APPROVAL,
        origin_user_receipt=_RECEIPT, action_digest=_DIGEST,
    )


def _read_admitted_status(fence, journal, *, delegated=False):
    from routes.gateway_notes_adapter import (
        read_admitted_notes_event_journal, read_lost_notes_review_status,
    )

    kind = "bearer" if delegated else "browser"
    principal = Principal(_OWNER, kind, frozenset({"chat"}) if delegated else frozenset())
    admission = GatewayAdmission().admit(
        principal, _SESSION,
        lambda sid: SessionOwnership(owner_id=_OWNER),
    )
    session = SimpleNamespace(
        id=_SESSION, owner=_OWNER, history=[
            SimpleNamespace(role="user", metadata={"_db_id": _RECEIPT}),
        ],
    )
    status = read_lost_notes_review_status(
        admission=admission, session=session, decision_fence=fence,
    )
    if delegated:
        try:
            read_admitted_notes_event_journal(
                admission=admission, session=session, journal=journal,
            )
        except AdmissionDenied:
            return {"status": status, "journal_denied": True}
        raise AssertionError("Delegated journal read unexpectedly accepted")
    events = read_admitted_notes_event_journal(
        admission=admission, session=session, journal=journal,
    )
    return {
        "status": status,
        "kinds": [event.kind for event in events],
        "cursors": [event.cursor for event in events],
        "foreign_events": len(journal.replay(
            owner="mallory", session_id=_SESSION,
            origin_user_receipt=_RECEIPT,
        )),
    }


def _worker(phase: str):
    engine = _trusted_test_database()
    try:
        fence = NotesDecisionFence(engine)
        journal = NotesEventJournal(engine)
        fence.require_schema()
        journal.require_schema()
        if phase == "issue":
            assert fence.issue(
                **_identity(), issued_at=_NOW - 1,
                expires_at=_NOW + 300, now=_NOW,
            )
            cursor = journal.append(
                owner=_OWNER, session_id=_SESSION, approval_id=_APPROVAL,
                origin_user_receipt=_RECEIPT, kind="review_registered", now=_NOW,
            )
            return {"issued": True, "cursor": cursor}
        if phase == "reserve":
            assert fence.reserve(
                **_identity(), decision="approve_task", now=_NOW + 1,
            )
            cursor = journal.append(
                owner=_OWNER, session_id=_SESSION, approval_id=_APPROVAL,
                origin_user_receipt=_RECEIPT, kind="decision_reserved",
                now=_NOW + 1,
            )
            return {"reserved": True, "cursor": cursor}
        if phase == "replay_attempt":
            denied = not fence.reserve(
                **_identity(), decision="approve_task", now=_NOW + 2,
            )
            return {"denied_duplicate": denied}
        if phase == "read":
            return _read_admitted_status(fence, journal)
        if phase == "read_delegated":
            return _read_admitted_status(fence, journal, delegated=True)
        raise ValueError("Unknown isolated restart phase")
    finally:
        engine.dispose()


def _invoke_fresh_process(engine, tmp_path: Path, phase: str):
    """Every invocation has its own interpreter, engine and run state."""
    root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MNERYS_NOTES_RESTART_ISOLATED": "1",
        "MNERYS_NOTES_RESTART_DSN": engine.url.render_as_string(hide_password=False),
        "MNERYS_NOTES_RESTART_ROOT": str(tmp_path),
    }
    if engine.dialect.name == "postgresql":
        env["MNERYS_M21_PG_TEST_ISOLATED"] = "1"
    run = subprocess.run(
        [sys.executable, "-m", "tests.test_mnerys_gateway_notes_restart_process",
         "--worker", phase],
        cwd=root, env=env, text=True, capture_output=True, timeout=22,
    )
    # Never print stderr: DBAPI failure messages can include credentials.
    assert run.returncode == 0, "Isolated recovery subprocess failed"
    return json.loads(run.stdout)


def test_restart_worker_refuses_unapproved_or_non_disposable_databases(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("MNERYS_NOTES_RESTART_ISOLATED", raising=False)
    with pytest.raises(RuntimeError, match="opt-in"):
        _trusted_test_database()

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    monkeypatch.setenv("MNERYS_NOTES_RESTART_ISOLATED", "1")
    monkeypatch.setenv("MNERYS_NOTES_RESTART_ROOT", str(allowed))
    monkeypatch.setenv(
        "MNERYS_NOTES_RESTART_DSN",
        "sqlite+pysqlite:///" + str(outside),
    )
    with pytest.raises(RuntimeError, match="non-disposable"):
        _trusted_test_database()

    monkeypatch.setenv(
        "MNERYS_NOTES_RESTART_DSN",
        "postgresql+psycopg2://fake:fake@127.0.0.1/unapproved_db",
    )
    monkeypatch.delenv("MNERYS_M21_PG_TEST_ISOLATED", raising=False)
    with pytest.raises(RuntimeError, match="non-isolated"):
        _trusted_test_database()


def test_real_process_restart_recovers_only_metadata_without_tool_replay(
    isolated_fence_engine, tmp_path,
):
    engine = isolated_fence_engine
    if engine.dialect.name == "sqlite" and engine.url.database in (None, ":memory:"):
        pytest.skip("Independent Python process cannot access SQLite memory")
    notes_event_journal_metadata().create_all(engine)
    try:
        issued = _invoke_fresh_process(engine, tmp_path, "issue")
        assert issued["issued"] is True
        assert len(issued["cursor"]) == 32

        # A brand-new process has no original in-memory run or signed approval.
        interrupted = _invoke_fresh_process(engine, tmp_path, "read")
        assert interrupted == {
            "status": "interrupted", "kinds": ["review_registered"],
            "cursors": [issued["cursor"]], "foreign_events": 0,
        }
        delegated = _invoke_fresh_process(engine, tmp_path, "read_delegated")
        assert delegated == {"status": None, "journal_denied": True}

        reservation = _invoke_fresh_process(engine, tmp_path, "reserve")
        assert reservation["reserved"] is True
        assert reservation["cursor"] != issued["cursor"]

        # Crash after reservation but before any effect receipt is ambiguous.
        # Another worker must report uncertainty and never reclaim the grant.
        uncertain = _invoke_fresh_process(engine, tmp_path, "read")
        assert uncertain == {
            "status": "indeterminate",
            "kinds": ["review_registered", "decision_reserved"],
            "cursors": [issued["cursor"], reservation["cursor"]],
            "foreign_events": 0,
        }
        assert _invoke_fresh_process(engine, tmp_path, "replay_attempt") == {
            "denied_duplicate": True,
        }
        assert _invoke_fresh_process(engine, tmp_path, "read") == uncertain
    finally:
        notes_event_journal_metadata().drop_all(engine)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Invalid isolated recovery test invocation")
    print(json.dumps(_worker(sys.argv[2]), separators=(",", ":")))
