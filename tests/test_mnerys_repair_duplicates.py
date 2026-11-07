"""Historical duplicate repair: classification, reversible hide, rollback.

The repair tool must only flag rows proven redundant by a completed durable
turn window; legitimate identical messages and ambiguous pairs stay visible.
Everything is reversible through --unhide, and the model-context loader must
never feed hidden rows back to the model.
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import core.session_manager as SM
from core.database import Base, utcnow_naive
from core.models import ChatMessage
from core.session_manager import SessionManager
from scripts.repair_duplicate_transcripts import (
    _classify,
    _hide_rows,
    _unhide_rows,
)


import os

from sqlalchemy.engine import make_url

PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def db_engine(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'repair.sqlite'}", connect_args={"timeout": 15},
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
            db_name, role, version = connection.execute(text(
                "SELECT current_database(), current_user,"
                " current_setting('server_version_num')",
            )).one()
        if (
            db_name != PG_DATABASE or role != PG_USER
            or not str(version).startswith("17")
        ):
            engine.dispose()
            raise RuntimeError(
                "Refusing to run against a non-isolated PostgreSQL 17 database",
            )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(SM, "SessionLocal", factory)
    import core.database as core_database

    monkeypatch.setattr(core_database, "SessionLocal", factory)
    manager = SessionManager()
    manager.sessions = {}
    manager.upload_handler = None
    monkeypatch.setattr("core.models._SESSION_MANAGER_INSTANCE", manager)
    yield engine, manager
    engine.dispose()


def _seed_completed_turn(engine, session_id, *, status="completed", with_terminal=True):
    # The turn window must contain the rows already written for it: it opens
    # a minute before and closes just after "now".
    now = utcnow_naive()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gateway_turns (id, owner, session_id, incarnation_id,"
            " status, created_at) VALUES (:i, 'alice', :s, 'inc', :st,"
            " :created)",
        ), {
            "i": uuid.uuid4().hex, "s": session_id, "st": status,
            "created": now - __import__("datetime").timedelta(minutes=1),
        })
        if with_terminal:
            conn.execute(text(
                "UPDATE gateway_turns SET terminal_at = :t WHERE session_id = :s"
                " AND status = :st",
            ), {"t": now + __import__("datetime").timedelta(seconds=1), "s": session_id, "st": status})


def _row(engine, row_id):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT metadata FROM chat_messages WHERE id = :i",
        ), {"i": row_id}).scalar_one()


def test_confirmed_pair_hidden_and_restored(db_engine):
    """Two identical assistant rows inside one completed turn window: the
    later row is hidden and --unhide restores it exactly."""
    engine, manager = db_engine
    manager.create_session("s1", "Repaired", "http://p.test/v1", "m1", owner="alice")
    manager.add_message("s1", ChatMessage("user", "2+2, respond with just the number"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    _seed_completed_turn(engine, "s1")

    report = _classify(engine)
    confirmed = [item for item in report if item["classification"] == "confirmed-redundant"]
    assert len(confirmed) == 1
    assert confirmed[0]["confidence"] == "high"

    hidden = _hide_rows(engine, [confirmed[0]["hide_row_id"]])
    assert hidden == [confirmed[0]["hide_row_id"]]
    metadata = json.loads(_row(engine, hidden[0]))
    assert metadata["hidden"] is True
    assert metadata["hidden_reason"] == "duplicate-write-repair"

    restored = _unhide_rows(engine, hidden)
    assert restored == hidden
    metadata = json.loads(_row(engine, hidden[0]))
    assert "hidden" not in metadata and "hidden_reason" not in metadata


def test_legitimate_repetition_stays_visible(db_engine):
    """Two identical assistant rows from DIFFERENT completed turns are
    legitimate repeated answers; the repair must not touch them."""
    engine, manager = db_engine
    manager.create_session("s1", "Legit", "http://p.test/v1", "m1", owner="alice")
    manager.add_message("s1", ChatMessage("user", "2+2, respond with just the number"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    _seed_completed_turn(engine, "s1")
    # A second, separate turn asking the same thing again.
    manager.add_message("s1", ChatMessage("user", "2+2, respond with just the number"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    _seed_completed_turn(engine, "s1")

    report = _classify(engine)
    confirmed = [item for item in report if item["classification"] == "confirmed-redundant"]
    assert confirmed == []
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM chat_messages WHERE session_id='s1' AND role='assistant'",
        )).scalar_one()
    assert count == 2


def test_ambiguous_pair_without_turn_window_stays_visible(db_engine):
    """Identical rows with no completed turn window covering both remain
    ambiguous and untouched."""
    engine, manager = db_engine
    manager.create_session("s1", "Ambiguous", "http://p.test/v1", "m1", owner="alice")
    manager.add_message("s1", ChatMessage("assistant", "orphan reply"))
    manager.add_message("s1", ChatMessage("assistant", "orphan reply"))

    report = _classify(engine)
    confirmed = [item for item in report if item["classification"] == "confirmed-redundant"]
    assert confirmed == []
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM chat_messages WHERE session_id='s1'",
        )).scalar_one()
    assert count == 2


def test_hidden_rows_never_reach_model_context(db_engine):
    """The model-context loader must skip hidden rows (they are exact
    duplicates; feeding them back would poison the conversation)."""
    engine, manager = db_engine
    manager.create_session("s1", "Context", "http://p.test/v1", "m1", owner="alice")
    manager.add_message("s1", ChatMessage("user", "2+2"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    duplicate = ChatMessage("assistant", "4", metadata={"hidden": True})
    manager.add_message("s1", duplicate)

    context = manager.get_session("s1").get_context_messages()
    contents = [entry.get("content") for entry in context if entry.get("role") == "assistant"]
    assert contents == ["4"]  # exactly one assistant entry reaches the model


def test_hide_is_idempotent_and_row_count_never_changes(db_engine):
    """Re-applying the hide must be a no-op, and hiding never deletes rows."""
    engine, manager = db_engine
    manager.create_session("s1", "Idem", "http://p.test/v1", "m1", owner="alice")
    manager.add_message("s1", ChatMessage("user", "2+2"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    manager.add_message("s1", ChatMessage("assistant", "4"))
    _seed_completed_turn(engine, "s1")

    report = _classify(engine)
    target = [
        item for item in report if item["classification"] == "confirmed-redundant"
    ][0]["hide_row_id"]
    first = _hide_rows(engine, [target])
    second = _hide_rows(engine, [target])
    assert first == [target] and second == []
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM chat_messages WHERE session_id='s1'",
        )).scalar_one()
    assert count == 3  # hide never deletes
