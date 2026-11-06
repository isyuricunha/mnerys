"""M2.2 restart regression: session fencing survives process boundaries.

A disposable file-backed SQLite database is written by this process, then a
brand-new Python interpreter reads the fenced row, tries a stale write and
an incarnation-guarded rename, and prints sanitized results. No production
database or configured environment is touched.
"""

import os
import subprocess
import sys
from pathlib import Path

from core.models import ChatMessage
from core.session_manager import SessionManager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import core.session_manager as SM


def _setup(database_path: str) -> None:
    engine = create_engine(f"sqlite:///{database_path}")
    from core.database import Base
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    SM.SessionLocal = factory
    manager = SessionManager()
    manager.sessions = {}
    manager.create_session(
        "s1", "Before", "https://example.com/v1", "m", owner="alice",
    )
    manager.add_message("s1", ChatMessage("user", "persisted before restart"))
    with engine.begin() as conn:
        revision, incarnation = conn.execute(text(
            "select revision, incarnation_id from sessions where id='s1'"
        )).fetchone()
    print(f"COMMITTED revision={revision} incarnation={incarnation}")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _stale_write(database_path: str) -> None:
    engine = create_engine(f"sqlite:///{database_path}")
    from core.database import Base
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    SM.SessionLocal = factory
    manager = SessionManager()
    manager.sessions = {}
    loaded = manager.get_session("s1")
    _assert(loaded.revision == 1, "restart must load the persisted revision")
    _assert(bool(loaded.incarnation_id), "restart must load the incarnation")
    # A writer reusing a stale fence snapshot (as a worker restart would if
    # it cached the row earlier) must be refused, not merged.
    fresh = manager._persist_message(
        "s1", ChatMessage("assistant", "late writer"),
    )
    _assert(fresh is True, "restarted manager must keep committing fresh turns")
    from core.session_manager import SessionRevisionConflict
    try:
        manager._apply_session_changes(
            factory(), "s1",
            {SM.DbSession.name: "StaleRenamed"},
            expected_owner="alice",
            expected_incarnation="not-the-current-incarnation",
        )
        _assert(False, "stale incarnation must refuse the write")
    except SessionRevisionConflict:
        pass
    with engine.connect() as conn:
        revision = conn.execute(text(
            "select revision from sessions where id='s1'"
        )).scalar()
        messages = conn.execute(text(
            "select count(*) from chat_messages where session_id='s1'"
        )).scalar()
    _assert(revision == 2, "fresh write must bump the revision exactly once")
    _assert(messages == 2, "fresh write must append exactly one message")
    print("STALE_REFUSED name=", end="")
    with engine.connect() as conn:
        print(conn.execute(text(
            "select name from sessions where id='s1'"
        )).scalar())


def main() -> int:
    database_path = sys.argv[1]
    mode = sys.argv[2]
    if mode == "setup":
        _setup(database_path)
    elif mode == "stale":
        _stale_write(database_path)
    else:
        raise SystemExit(f"Unknown mode {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_restart_fencing_with_fresh_interpreters(tmp_path):
    database_path = str(tmp_path / "restart.sqlite")
    env = dict(os.environ, DATABASE_URL="sqlite:///:memory:")
    this = Path(__file__).resolve()
    repo = this.parents[1]
    setup = subprocess.run(
        [sys.executable, str(this), database_path, "setup"],
        capture_output=True, text=True, timeout=120, env=env, cwd=repo,
        check=True,
    )
    assert "COMMITTED revision=1" in setup.stdout
    stale = subprocess.run(
        [sys.executable, str(this), database_path, "stale"],
        capture_output=True, text=True, timeout=120, env=env, cwd=repo,
        check=True,
    )
    assert "STALE_REFUSED name=Before" in stale.stdout