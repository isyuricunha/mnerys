"""Regression coverage for the inherited Session / SessionManager append cache.

No real Mnerys database is accessed: the fixture creates disposable SQLite
tables. The existing public append methods remain unchanged and no revision
migration is involved.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core import database, models
import core.session_manager as session_manager_module
from core.models import ChatMessage, Session


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache-append.sqlite'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(session_manager_module, "SessionLocal", factory)
    manager = session_manager_module.SessionManager.__new__(
        session_manager_module.SessionManager
    )
    manager.sessions = {}
    manager.upload_handler = None
    cached = Session(
        id="s1", name="Cache rollback", endpoint_url="http://example.com",
        model="test", owner="alice",
    )
    manager.sessions[cached.id] = cached
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", manager)
    # The production manager normally reloads/touches before returning; for
    # this unit test the cached object is already fully hydrated.
    monkeypatch.setattr(manager, "get_session", lambda sid: manager.sessions[sid])
    with factory() as db:
        db.add(database.Session(
            id="s1", name="Cache rollback", model="test",
            endpoint_url="http://example.com", owner="alice", message_count=0,
        ))
        db.commit()
    try:
        yield manager, cached, factory, engine
    finally:
        engine.dispose()


def durable(store):
    _, _, factory, _ = store
    with factory() as db:
        rows = db.query(database.ChatMessage).filter_by(session_id="s1").all()
        parent = db.get(database.Session, "s1")
        return rows, parent.message_count if parent else None


def deny_inserts(engine):
    def reject(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("INSERT INTO CHAT_MESSAGES"):
            raise RuntimeError("simulated insert failure")
    event.listen(engine, "before_cursor_execute", reject)
    return reject


def test_session_add_message_reverts_failed_insert_without_replacing_history(store):
    manager, cached, _, engine = store
    old_history = cached.history
    msg = ChatMessage("user", "not stored", metadata={"source": "test"})
    reject = deny_inserts(engine)
    try:
        cached.add_message(msg)
    finally:
        event.remove(engine, "before_cursor_execute", reject)
    assert cached.history is old_history
    assert cached.history == []
    assert cached.message_count == 0
    assert "_db_id" not in msg.metadata
    assert durable(store)[0] == []
    assert durable(store)[1] == 0


def test_manager_add_message_reverts_failed_insert(store):
    manager, cached, _, engine = store
    old_history = cached.history
    msg = ChatMessage("assistant", "not stored", metadata={})
    reject = deny_inserts(engine)
    try:
        manager.add_message("s1", msg)
    finally:
        event.remove(engine, "before_cursor_execute", reject)
    assert cached.history is old_history
    assert len(cached.history) == 0
    assert cached._history is old_history
    assert cached.message_count == 0
    assert durable(store)[0] == []
    assert "_db_id" not in msg.metadata


@pytest.mark.parametrize("entrypoint", ["session", "manager"])
def test_successful_append_still_commits_and_exposes_message_id(store, entrypoint):
    manager, cached, _, _ = store
    msg = ChatMessage("assistant", "saved", metadata={"kind": "reply"})
    if entrypoint == "session":
        cached.add_message(msg)
    else:
        manager.add_message("s1", msg)
    rows, db_count = durable(store)
    assert len(rows) == 1
    assert rows[0].content == "saved"
    assert rows[0].id == msg.metadata["_db_id"]
    assert cached.history == [msg]
    assert cached.message_count == 1
    assert db_count == 1


def test_deleted_parent_does_not_leave_orphan_in_session_history(store):
    manager, cached, factory, _ = store
    with factory() as db:
        db.delete(db.get(database.Session, "s1"))
        db.commit()
    msg = ChatMessage("assistant", "after delete", metadata={})
    cached.add_message(msg)
    assert "s1" not in manager.sessions
    assert cached.history == []
    assert cached.message_count == 0
    assert "_db_id" not in msg.metadata
    assert durable(store) == ([], None)


def test_rollback_removes_by_identity_without_removing_later_messages():
    first = ChatMessage("user", "duplicate")
    failed = ChatMessage("user", "duplicate")
    later = ChatMessage("assistant", "later")
    session = Session(
        id="s", name="Ephemeral", endpoint_url="http://example.com",
        model="test", history=[first, failed, later], message_count=3,
    )
    original = session.history
    session._discard_failed_message(failed)
    assert original is session.history
    assert session.history == [first, later]
    assert session.history[0] is first
    assert session.message_count == 2


def test_ephemeral_session_still_appends_without_manager(monkeypatch):
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", None)
    session = Session(
        id="local", name="Local", endpoint_url="http://example.com",
        model="test",
    )
    msg = ChatMessage("user", "ephemeral")
    session.add_message(msg)
    assert session.history == [msg]
    assert session.message_count == 1


def test_failed_append_followed_by_success_keeps_db_and_cache_in_sync(store):
    manager, cached, _, engine = store
    rejected = ChatMessage("user", "failed", metadata={})
    reject = deny_inserts(engine)
    try:
        cached.add_message(rejected)
    finally:
        event.remove(engine, "before_cursor_execute", reject)

    accepted = ChatMessage("user", "accepted", metadata={})
    cached.add_message(accepted)

    rows, db_count = durable(store)
    assert [row.content for row in rows] == ["accepted"]
    assert cached.history == [accepted]
    assert cached.message_count == db_count == 1
    assert "_db_id" not in rejected.metadata
    assert accepted.metadata["_db_id"] == rows[0].id
