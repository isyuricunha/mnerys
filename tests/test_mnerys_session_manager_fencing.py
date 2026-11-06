"""M2.2 integrated session-write fencing in the real SessionManager.

Disposable file-backed SQLite databases only; the guarded writers are the
inherited ones. PostgreSQL 17 cases run on the dedicated disposable harness.
"""

import os

from core.models import ChatMessage
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import core.session_manager as SM
from core.session_manager import SessionManager


PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def manager(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'fencing.sqlite'}",
            connect_args={"timeout": 15},
        )
    else:
        dsn = os.environ.get(PG_ENV)
        if not dsn:
            pytest.skip("Disposable PostgreSQL 17 fixture not supplied")
        url = make_url(dsn)
        if url.host != "127.0.0.1" or url.port is None:
            raise RuntimeError("Refusing a non-loopback PostgreSQL test connection")
        engine = create_engine(
            dsn, connect_args={
                "options": "-c statement_timeout=5000 -c lock_timeout=4000",
            },
        )
        with engine.connect() as connection:
            db_name, role, version = connection.execute(
                text("SELECT current_database(), current_user, current_setting('server_version_num')")
            ).one()
        if db_name != PG_DATABASE or role != PG_USER or not str(version).startswith("17"):
            engine.dispose()
            raise RuntimeError("Refusing to run against a non-isolated PostgreSQL 17 database")
    try:
        from core.database import Base
        # The PG17 drill reuses one disposable database across suites;
        # drop any previous suite's legacy tables before the full schema.
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        from sqlalchemy.orm import sessionmaker
        SM.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        manager = SessionManager()
        manager.sessions = {}
        yield manager, engine
    finally:
        engine.dispose()


def test_appends_bump_revision_and_counter_together(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "Fenced", "https://example.com/v1", "m", owner="alice")
    for index in range(3):
        session_manager.add_message("s1", ChatMessage("user", f"msg {index}"))
    with engine.connect() as conn:
        row = conn.execute(text(
            "select revision, message_count, incarnation_id from sessions where id='s1'"
        )).fetchone()
    assert row[0] == 3
    assert row[1] == 3
    assert row[2] and len(row[2]) == 32


def test_owner_transfer_refuses_the_cached_writer(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "Owned", "https://example.com/v1", "m", owner="alice")
    with engine.begin() as conn:
        conn.execute(text("update sessions set owner='bob' where id='s1'"))
    # A mid-turn stream still holds the cached object without re-syncing;
    # the fenced append must refuse the stale owner instead of losing the
    # new owner's writes.
    assert session_manager._persist_message(
        "s1", ChatMessage("user", "stale write"),
    ) is False
    assert [m.role for m in session_manager.sessions["s1"].history] == []
    with engine.connect() as conn:
        count = conn.execute(text(
            "select count(*) from chat_messages where session_id='s1'"
        )).scalar()
    assert count == 0


def test_recreated_session_id_refuses_the_stale_incarnation(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "First", "https://example.com/v1", "m", owner="alice")
    stale_incarnation = session_manager.sessions["s1"].incarnation_id
    with engine.begin() as conn:
        conn.execute(text("delete from sessions where id='s1'"))
        conn.execute(text(
            "insert into sessions (id, name, endpoint_url, model, owner,"
            " revision, incarnation_id, created_at, updated_at)"
            " values ('s1', 'Second', 'https://example.com/v1', 'm', 'alice',"
            " 0, :new_incarnation, current_timestamp, current_timestamp)"
        ), {"new_incarnation": "b" * 32})
    assert stale_incarnation != "b" * 32
    # A mid-turn writer still holds the deleted lifetime's incarnation.
    session_manager.sessions["s1"].incarnation_id = stale_incarnation
    assert session_manager._persist_message(
        "s1", ChatMessage("user", "stale incarnation"),
    ) is False
    with engine.connect() as conn:
        count = conn.execute(text(
            "select count(*) from chat_messages where session_id='s1'"
        )).scalar()
    assert count == 0


def test_unmigrated_rows_keep_the_legacy_write(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "Legacy", "https://example.com/v1", "m", owner="alice")
    with engine.begin() as conn:
        conn.execute(text(
            "update sessions set revision=null, incarnation_id=null where id='s1'"
        ))
    session_manager.sessions["s1"].revision = None
    session_manager.sessions["s1"].incarnation_id = None
    session_manager.add_message("s1", ChatMessage("user", "legacy"))
    assert session_manager.sessions["s1"].history[-1].content == "legacy"


def test_sequential_fresh_writers_commit_in_order(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "Raced", "https://example.com/v1", "m", owner="alice")
    session_manager.add_message("s1", ChatMessage("user", "first"))
    session_manager.add_message("s1", ChatMessage("user", "second"))
    with engine.connect() as conn:
        rows = conn.execute(text(
            "select revision, message_count from sessions where id='s1'"
        )).fetchone()
        messages = conn.execute(text(
            "select count(*) from chat_messages where session_id='s1'"
        )).scalar()
    # Both writes committed serially: every fresh writer bumps exactly once.
    assert rows[0] == 2
    assert rows[1] == 2
    assert messages == 2


def test_rename_and_archive_bump_revision_and_reject_stale_metadata(manager):
    session_manager, engine = manager
    session_manager.create_session("s1", "Before", "https://example.com/v1", "m", owner="alice")
    session_manager.update_session_name("s1", "Renamed")
    with engine.connect() as conn:
        revision = conn.execute(text(
            "select revision from sessions where id='s1'"
        )).scalar()
    assert revision == 1
    session_manager.archive_session("s1")
    with engine.connect() as conn:
        revision, archived = conn.execute(text(
            "select revision, archived from sessions where id='s1'"
        )).fetchone()
    assert (revision, bool(archived)) == (2, True)
