"""Regression for single-commit, owner-scoped agent-session rename.

All tables are temporary SQLite fixtures. The tool still inherits its current
authorization context from the agent runtime; no new tool permissions are
introduced here and no revision migration is activated.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core import database
import src.database as src_database
from src.agent_tools import session_tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'session-tool-rename.sqlite'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(src_database, "SessionLocal", factory)

    def manager():
        raise AssertionError("Legacy manager DB rename must not be called")

    cached = {
        "mine": SimpleNamespace(
            owner="alice", name="Old", archived=False, is_important=False,
        ),
        "foreign": SimpleNamespace(
            owner="bob", name="Foreign", archived=False, is_important=False,
        ),
    }
    mgr = SimpleNamespace(sessions=cached, update_session_name=manager)
    monkeypatch.setattr(session_tools, "get_session_manager", lambda: mgr)
    with factory() as db:
        db.add_all([
            database.Session(
                id="mine", owner="alice", name="Old", model="test",
                endpoint_url="http://example.com",
            ),
            database.Session(
                id="foreign", owner="bob", name="Foreign", model="test",
                endpoint_url="http://example.com",
            ),
        ])
        db.commit()
    try:
        yield engine, factory, mgr
    finally:
        engine.dispose()


def rename(sid, name, owner):
    return asyncio.run(session_tools.manage_session(
        '{"action":"rename","session_id":"' + sid + '","name":"' + name + '"}',
        session_id="other", owner=owner,
    ))


def manage(sid, action, owner):
    return asyncio.run(session_tools.manage_session(
        '{"action":"' + action + '","session_id":"' + sid + '"}',
        session_id="other", owner=owner,
    ))


def get_row(factory, sid):
    with factory() as db:
        return db.get(database.Session, sid)


def test_rename_issues_only_one_owner_scoped_db_update(store):
    engine, factory, mgr = store
    updates = []

    def record(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE SESSIONS"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = rename("mine", "New", "alice")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert result["action"] == "rename"
    assert result["name"] == "New"
    assert get_row(factory, "mine").name == "New"
    assert mgr.sessions["mine"].name == "New"
    assert get_row(factory, "foreign").name == "Foreign"
    assert len(updates) == 1


def test_authenticated_tool_cannot_rename_foreign_session(store):
    _, factory, mgr = store
    result = rename("foreign", "Forbidden", "alice")
    assert "not found" in result["error"]
    assert get_row(factory, "foreign").name == "Foreign"
    assert mgr.sessions["foreign"].name == "Foreign"


def test_failed_db_commit_does_not_change_cached_name(store):
    engine, factory, mgr = store

    def refuse(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE SESSIONS"):
            raise RuntimeError("simulated write failure")

    event.listen(engine, "before_cursor_execute", refuse)
    try:
        result = rename("mine", "Uncommitted", "alice")
    finally:
        event.remove(engine, "before_cursor_execute", refuse)

    assert "error" in result
    assert get_row(factory, "mine").name == "Old"
    assert mgr.sessions["mine"].name == "Old"


def test_stale_foreign_cache_entry_is_not_modified(store):
    _, factory, mgr = store
    mgr.sessions["mine"].owner = "bob"
    result = rename("mine", "Committed", "alice")
    assert result["action"] == "rename"
    assert get_row(factory, "mine").name == "Committed"
    assert mgr.sessions["mine"].name == "Old"


def test_single_user_null_owner_session_cache_refresh(store):
    _, factory, mgr = store
    with factory() as db:
        db.add(database.Session(
            id="single-user", owner=None, name="Local", model="test",
            endpoint_url="http://example.com",
        ))
        db.commit()
    mgr.sessions["single-user"] = SimpleNamespace(owner=None, name="Local")
    result = rename("single-user", "Renamed", None)
    assert result["action"] == "rename"
    assert get_row(factory, "single-user").name == "Renamed"
    assert mgr.sessions["single-user"].name == "Renamed"


@pytest.mark.parametrize(
    ("action", "field", "expected"),
    [
        ("archive", "archived", True),
        ("unarchive", "archived", False),
        ("important", "is_important", True),
    ],
)
def test_metadata_action_refreshes_matching_cache_after_commit(
    store, action, field, expected,
):
    _, factory, mgr = store
    result = manage("mine", action, "alice")
    assert result["action"] == action
    assert getattr(get_row(factory, "mine"), field) is expected
    assert getattr(mgr.sessions["mine"], field) is expected


def test_unimportant_cannot_remove_user_star(store):
    _, factory, mgr = store
    assert manage("mine", "important", "alice")["action"] == "important"
    blocked = manage("mine", "unimportant", "alice")
    assert "Only the user" in blocked["error"]
    assert get_row(factory, "mine").is_important is True
    assert mgr.sessions["mine"].is_important is True


@pytest.mark.parametrize("action", ["archive", "important"])
def test_foreign_owner_cannot_change_metadata_or_cached_state(
    store, action,
):
    _, factory, mgr = store
    result = manage("foreign", action, "alice")
    assert "not found" in result["error"]
    assert get_row(factory, "foreign").archived is False
    assert get_row(factory, "foreign").is_important is False
    assert mgr.sessions["foreign"].archived is False
    assert mgr.sessions["foreign"].is_important is False


def test_archive_failure_keeps_cached_and_database_flags_unchanged(store):
    engine, factory, mgr = store

    def reject(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE SESSIONS"):
            raise RuntimeError("simulated archive failure")

    event.listen(engine, "before_cursor_execute", reject)
    try:
        result = manage("mine", "archive", "alice")
    finally:
        event.remove(engine, "before_cursor_execute", reject)

    assert "error" in result
    assert get_row(factory, "mine").archived is False
    assert mgr.sessions["mine"].archived is False
