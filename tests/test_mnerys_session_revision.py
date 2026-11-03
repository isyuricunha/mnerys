"""M1 isolated SQLite CAS contract tests.

These tables are test-only and created under pytest's tmp_path; the real
sessions schema and production data are not touched. SQLite supplies a real
SQLAlchemy backend rather than an in-memory imitation of UPDATE/rowcount.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.gateway_admission import GatewayAdmission, Principal, SessionOwnership
from src.session_revision import (
    SessionMetadataChanges,
    SessionRevisionConflict,
    SessionRevisionUnavailable,
    SqlSessionRevisionWriter,
)


Base = declarative_base()


class RevisionFixtureSession(Base):
    """A hypothetical *migrated* sessions row; not the live Session model."""

    __tablename__ = "m1_test_revision_sessions"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    name = Column(String, nullable=False)
    archived = Column(Boolean, nullable=False, default=False)
    is_important = Column(Boolean, nullable=False, default=False)


@pytest.fixture
def db(tmp_path):
    """Isolated file-backed SQLite allows independent concurrent connections."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'revision.sqlite'}",
        connect_args={"timeout": 5.0},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([
            RevisionFixtureSession(
                id="s1", owner="alice", name="First", revision=0,
                archived=False, is_important=False,
            ),
            RevisionFixtureSession(
                id="s2", owner="bob", name="Second", revision=0,
                archived=False, is_important=False,
            ),
            RevisionFixtureSession(
                id="legacy", owner=None, name="Legacy", revision=0,
                archived=False, is_important=False,
            ),
        ])
        session.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def admitted(session="s1", owner="alice", kind="browser"):
    principal = Principal(
        owner, kind, frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session, lambda _: SessionOwnership(owner),
    )


def row(factory, sid="s1"):
    with factory() as session:
        return session.query(RevisionFixtureSession).filter_by(id=sid).one()


def test_read_revision_and_atomic_metadata_patch(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = admitted()
    assert writer.current_revision(grant) == 0
    assert writer.patch(
        grant, expected_revision=0,
        changes=SessionMetadataChanges(name="New title", archived=True),
    ) == 1
    updated = row(db)
    assert (updated.name, updated.archived, updated.is_important, updated.revision) == (
        "New title", True, False, 1,
    )
    assert writer.current_revision(grant) == 1


def test_outdated_revision_never_overwrites_committed_change(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = admitted()
    assert writer.patch(
        grant, expected_revision=0,
        changes=SessionMetadataChanges(name="Newer"),
    ) == 1
    with pytest.raises(SessionRevisionConflict):
        writer.patch(
            grant, expected_revision=0,
            changes=SessionMetadataChanges(name="Older"),
        )
    assert (row(db).name, row(db).revision) == ("Newer", 1)


def test_stale_admission_cannot_mutate_newly_reassigned_owner(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    old_owner = admitted()
    with db() as session:
        session.query(RevisionFixtureSession).filter_by(id="s1").update(
            {"owner": "bob"}, synchronize_session=False,
        )
        session.commit()

    assert writer.current_revision(old_owner) is None
    with pytest.raises(SessionRevisionConflict):
        writer.patch(
            old_owner, expected_revision=0,
            changes=SessionMetadataChanges(name="Forbidden"),
        )
    assert row(db).name == "First"


def test_undiscoverable_foreign_or_deleted_session_is_never_written(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    alice = admitted(session="s2", owner="alice")
    assert writer.current_revision(alice) is None
    with pytest.raises(SessionRevisionConflict):
        writer.patch(
            alice, expected_revision=0,
            changes=SessionMetadataChanges(archived=True),
        )
    assert not row(db, "s2").archived
    with db() as session:
        session.query(RevisionFixtureSession).filter_by(id="s1").delete()
        session.commit()
    with pytest.raises(SessionRevisionConflict):
        writer.patch(
            admitted(), expected_revision=0,
            changes=SessionMetadataChanges(archived=True),
        )


def test_authenticated_single_user_mode_works_for_legacy_owner(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = GatewayAdmission().admit(
        Principal(None, "single_user"), "legacy",
        lambda _: SessionOwnership(None),
    )
    assert writer.patch(
        grant, expected_revision=0,
        changes=SessionMetadataChanges(is_important=True),
    ) == 1
    assert (row(db, "legacy").is_important, row(db, "legacy").revision) == (True, 1)


def test_bearer_client_cannot_gain_more_than_owner_scoped_access(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = admitted(kind="bearer")
    assert writer.patch(
        grant, expected_revision=0,
        changes=SessionMetadataChanges(name="From scoped token"),
    ) == 1
    assert row(db).name == "From scoped token"
    assert row(db, "s2").name == "Second"


def test_two_concurrent_writers_same_revision_have_one_winner(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = admitted()
    gate = Barrier(2)

    def attempt(name):
        gate.wait(timeout=5)
        try:
            writer.patch(
                grant, expected_revision=0,
                changes=SessionMetadataChanges(name=name),
            )
            return "committed"
        except SessionRevisionConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(attempt, "First worker")
        f2 = pool.submit(attempt, "Second worker")
        results = [f1.result(timeout=10), f2.result(timeout=10)]
    assert sorted(results) == ["committed", "stale"]
    assert row(db).revision == 1
    assert row(db).name in {"First worker", "Second worker"}


def test_invalid_revisions_and_empty_changes_fail_before_db_write(db):
    writer = SqlSessionRevisionWriter(db, RevisionFixtureSession)
    grant = admitted()
    for invalid in (True, -1, "0", None):
        with pytest.raises(ValueError):
            writer.patch(
                grant, expected_revision=invalid,
                changes=SessionMetadataChanges(archived=True),
            )
    with pytest.raises(ValueError):
        SessionMetadataChanges()
    with pytest.raises(ValueError):
        SessionMetadataChanges(name="")
    with pytest.raises(ValueError):
        SessionMetadataChanges(archived="yes")
    assert row(db).revision == 0


def test_legacy_model_without_revision_fails_before_opening_session():
    class LegacyModel:
        pass

    def must_not_connect():
        raise AssertionError("Legacy schema must not be queried or modified")

    writer = SqlSessionRevisionWriter(must_not_connect, LegacyModel)
    with pytest.raises(SessionRevisionUnavailable):
        writer.current_revision(admitted())
    with pytest.raises(SessionRevisionUnavailable):
        writer.patch(
            admitted(), expected_revision=0,
            changes=SessionMetadataChanges(name="Forbidden"),
        )


def test_exception_after_guarded_update_rolls_back_entire_transaction(db):
    class CommitFailingSession:
        def __init__(self):
            self.inner = db()
        def query(self, *args, **kwargs):
            return self.inner.query(*args, **kwargs)
        def commit(self):
            raise RuntimeError("simulated commit failure")
        def rollback(self):
            return self.inner.rollback()
        def close(self):
            return self.inner.close()

    writer = SqlSessionRevisionWriter(CommitFailingSession, RevisionFixtureSession)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        writer.patch(
            admitted(), expected_revision=0,
            changes=SessionMetadataChanges(name="Must roll back"),
        )
    assert row(db).name == "First"
    assert row(db).revision == 0
