"""M1 opt-in session writer tests: SQLite and dedicated, disposable PostgreSQL 17.

PostgreSQL cases require MNERYS_M1_PG_TEST_DSN, supplied only by the local
ephemeral-container harness. The fixture refuses any other database name/user
before creating tables. Never point these tests at a production database.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal, SessionOwnership
from src.session_revision import (
    SessionMetadataChanges, SessionRevisionConflict, SessionRevisionUnavailable,
)
from src.session_write_fencing import (
    PreparedSessionMessage, SessionWriteFence, SqlFencedSessionWriter,
)


Base = declarative_base()
PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"


class SessionFixture(Base):
    __tablename__ = "m1_fenced_sessions"
    id = Column(String, primary_key=True)
    owner = Column(String)
    incarnation_id = Column(String, nullable=False)
    revision = Column(Integer, nullable=False, default=0)
    name = Column(String, nullable=False)
    archived = Column(Boolean, nullable=False, default=False)
    is_important = Column(Boolean, nullable=False, default=False)
    message_count = Column(Integer, nullable=False, default=0)
    last_accessed = Column(DateTime)
    last_message_at = Column(DateTime)


class MessageFixture(Base):
    __tablename__ = "m1_fenced_messages"
    id = Column(String, primary_key=True)
    session_id = Column(
        String, ForeignKey("m1_fenced_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    meta_data = Column("metadata", Text)
    timestamp = Column(DateTime, nullable=False)


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def database(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'fenced-sessions.sqlite'}",
            connect_args={"timeout": 10},
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
        # Safety gate. Re-check the authenticated server identity before any
        # DDL or data mutation, regardless of what the DSN claims to point to.
        with engine.connect() as connection:
            db_name, role, version = connection.execute(
                text("SELECT current_database(), current_user, current_setting('server_version_num')")
            ).one()
        if db_name != PG_DATABASE or role != PG_USER or not str(version).startswith("17"):
            engine.dispose()
            raise RuntimeError("Refusing to run against a non-isolated PostgreSQL 17 database")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            session.add_all([
                SessionFixture(
                    id="s1", owner="alice", incarnation_id="epoch-a",
                    revision=0, name="First", message_count=0,
                ),
                SessionFixture(
                    id="s2", owner="bob", incarnation_id="epoch-b",
                    revision=0, name="Second", message_count=0,
                ),
                SessionFixture(
                    id="legacy", owner=None, incarnation_id="epoch-legacy",
                    revision=0, name="Legacy", message_count=0,
                ),
            ])
            session.commit()
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def admitted(session_id="s1", owner="alice", *, kind="browser"):
    principal = Principal(
        owner, kind, frozenset({"chat"}) if kind == "bearer" else frozenset()
    )
    return GatewayAdmission().admit(
        principal, session_id, lambda _: SessionOwnership(owner),
    )


def row(factory, session_id="s1"):
    with factory() as session:
        return session.query(SessionFixture).filter_by(id=session_id).one()


def messages(factory, session_id="s1"):
    with factory() as session:
        return session.query(MessageFixture).filter_by(session_id=session_id).all()


def writer(factory):
    return SqlFencedSessionWriter(factory, SessionFixture, MessageFixture)


def test_fenced_metadata_and_message_transaction(database):
    adapter = writer(database)
    grant = admitted()
    fence = adapter.read_fence(grant)
    assert fence == SessionWriteFence("s1", "epoch-a", 0)

    new_fence = adapter.patch_metadata(
        grant, fence, SessionMetadataChanges(name="Renamed", archived=True),
    )
    assert new_fence == SessionWriteFence("s1", "epoch-a", 1)

    next_fence, message_id = adapter.append_prepared_message(
        grant, new_fence,
        PreparedSessionMessage("assistant", "sanitized hello", '{"origin":"test"}'),
    )
    assert next_fence == SessionWriteFence("s1", "epoch-a", 2)
    state = row(database)
    assert (state.revision, state.incarnation_id, state.name, state.archived) == (
        2, "epoch-a", "Renamed", True,
    )
    assert state.message_count == 1
    assert state.last_message_at is not None
    assert state.last_accessed is not None
    stored = messages(database)
    assert len(stored) == 1
    assert (stored[0].id, stored[0].role, stored[0].content, stored[0].meta_data) == (
        message_id, "assistant", "sanitized hello", '{"origin":"test"}',
    )


def test_stale_message_cannot_duplicate_or_modify_counter(database):
    adapter, grant = writer(database), admitted()
    fence = adapter.read_fence(grant)
    adapter.append_prepared_message(
        grant, fence, PreparedSessionMessage("user", "first"),
    )
    with pytest.raises(SessionRevisionConflict):
        adapter.append_prepared_message(
            grant, fence, PreparedSessionMessage("user", "should not persist"),
        )
    assert row(database).message_count == 1
    assert row(database).revision == 1
    assert [m.content for m in messages(database)] == ["first"]


def test_deleted_and_recreated_session_rejects_old_incarnation(database):
    adapter, grant = writer(database), admitted()
    old_fence = adapter.read_fence(grant)
    with database() as session:
        session.query(SessionFixture).filter_by(id="s1").delete()
        session.commit()
    with database() as session:
        session.add(SessionFixture(
            id="s1", owner="alice", incarnation_id="epoch-new",
            revision=0, name="Recreated", message_count=0,
        ))
        session.commit()

    assert adapter.read_fence(grant) == SessionWriteFence("s1", "epoch-new", 0)
    with pytest.raises(SessionRevisionConflict):
        adapter.patch_metadata(
            grant, old_fence, SessionMetadataChanges(name="Stale write"),
        )
    with pytest.raises(SessionRevisionConflict):
        adapter.append_prepared_message(
            grant, old_fence, PreparedSessionMessage("assistant", "stale"),
        )
    assert row(database).name == "Recreated"
    assert row(database).revision == 0
    assert messages(database) == []


def test_owner_change_after_admission_rejects_stale_fence(database):
    adapter, grant = writer(database), admitted()
    fence = adapter.read_fence(grant)
    with database() as session:
        session.query(SessionFixture).filter_by(id="s1").update(
            {"owner": "bob"}, synchronize_session=False,
        )
        session.commit()
    assert adapter.read_fence(grant) is None
    with pytest.raises(SessionRevisionConflict):
        adapter.append_prepared_message(
            grant, fence, PreparedSessionMessage("assistant", "not authorized"),
        )
    assert row(database).message_count == 0


def test_foreign_session_looks_missing(database):
    adapter = writer(database)
    foreign_grant = admitted("s2", "alice")
    assert adapter.read_fence(foreign_grant) is None
    with pytest.raises(SessionRevisionConflict):
        adapter.patch_metadata(
            foreign_grant,
            SessionWriteFence("s2", "epoch-b", 0),
            SessionMetadataChanges(archived=True),
        )
    assert not row(database, "s2").archived


def test_concurrent_message_writes_from_same_revision_only_one_commits(database):
    adapter, grant = writer(database), admitted()
    fence = adapter.read_fence(grant)
    start = Barrier(2)

    def attempt(message):
        start.wait(timeout=5)
        try:
            adapter.append_prepared_message(
                grant, fence, PreparedSessionMessage("user", message),
            )
            return "committed"
        except SessionRevisionConflict:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(attempt, "First worker")
        b = pool.submit(attempt, "Second worker")
        result = sorted([a.result(timeout=12), b.result(timeout=12)])
    assert result == ["committed", "rejected"]
    assert row(database).revision == 1
    assert row(database).message_count == 1
    assert len(messages(database)) == 1


def test_message_insert_failure_rolls_back_revision_and_counter(database, monkeypatch):
    adapter, grant = writer(database), admitted()
    fence = adapter.read_fence(grant)
    monkeypatch.setattr(
        "src.session_write_fencing.uuid4",
        lambda: SimpleNamespace(hex="fixed-test-message-id"),
    )
    fence, _ = adapter.append_prepared_message(
        grant, fence, PreparedSessionMessage("assistant", "first"),
    )
    with pytest.raises(IntegrityError):
        adapter.append_prepared_message(
            grant, fence, PreparedSessionMessage("assistant", "duplicate id"),
        )
    state = row(database)
    assert state.revision == 1
    assert state.message_count == 1
    assert [m.content for m in messages(database)] == ["first"]


def test_fence_from_other_session_is_denied(database):
    adapter, grant = writer(database), admitted()
    fence = adapter.read_fence(grant)
    with pytest.raises(SessionRevisionConflict):
        adapter.patch_metadata(
            grant, replace(fence, session_id="s2"),
            SessionMetadataChanges(name="No"),
        )
    assert row(database).revision == 0


def test_single_user_legacy_row_with_explicit_principal(database):
    adapter = writer(database)
    grant = GatewayAdmission().admit(
        Principal(None, "single_user"), "legacy",
        lambda _: SessionOwnership(None),
    )
    fence = adapter.read_fence(grant)
    assert fence.incarnation_id == "epoch-legacy"
    next_fence = adapter.patch_metadata(
        grant, fence, SessionMetadataChanges(is_important=True),
    )
    assert next_fence.revision == 1
    assert row(database, "legacy").is_important


def test_legacy_schema_and_invalid_fence_fail_before_database_connection():
    class InheritedSession:
        pass

    def forbidden():
        raise AssertionError("Must not connect to the inherited schema")

    adapter = SqlFencedSessionWriter(forbidden, InheritedSession, object)
    with pytest.raises(SessionRevisionUnavailable):
        adapter.read_fence(admitted())
    with pytest.raises(SessionRevisionUnavailable):
        adapter.append_prepared_message(
            admitted(), SessionWriteFence("s1", "epoch-a", 0),
            PreparedSessionMessage("user", "test"),
        )
    with pytest.raises(ValueError):
        SessionWriteFence("s1", "", 0)
    with pytest.raises(ValueError):
        SessionWriteFence("s1", "epoch", True)
    with pytest.raises(ValueError):
        PreparedSessionMessage("", "text")


def test_invalid_admission_cannot_write(database):
    adapter = writer(database)
    for invalid in (None, "s1", replace(admitted(), session_id="s2")):
        with pytest.raises(AdmissionDenied):
            adapter.patch_metadata(
                invalid, SessionWriteFence("s1", "epoch-a", 0),
                SessionMetadataChanges(name="No"),
            )
    assert row(database).name == "First"
