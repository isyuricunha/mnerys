"""PostgreSQL 17-only, disposable legacy-schema migration preview.

This module never mutates the application's sessions table or existing DB.
It requires a dedicated localhost PostgreSQL 17 container/role/database and
uses only uniquely prefixed synthetic tables for its test migration.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

PG_ENV = "MNERYS_M1_PG_TEST_DSN"


@pytest.fixture
def pg_legacy():
    dsn = os.getenv(PG_ENV)
    if not dsn:
        pytest.skip("Disposable PostgreSQL 17 fixture not supplied")
    url = make_url(dsn)
    if url.host != "127.0.0.1" or url.port is None:
        raise RuntimeError("Refusing a non-loopback PostgreSQL test connection")
    engine = create_engine(
        dsn,
        connect_args={
            "options": "-c statement_timeout=5000 -c lock_timeout=4000",
        },
    )
    with engine.connect() as connection:
        db_name, role, version = connection.execute(text(
            "SELECT current_database(), current_user, current_setting('server_version_num')"
        )).one()
    if (db_name, role) != ("mnerys_m1_isolated", "mnerys_m1_test") or not str(version).startswith("17"):
        engine.dispose()
        raise RuntimeError("Refusing to preview migration outside disposable PostgreSQL 17")

    try:
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE m1_migration_preview_sessions (
                    id VARCHAR(80) PRIMARY KEY,
                    owner VARCHAR(80),
                    name VARCHAR(120) NOT NULL,
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
            """))
            connection.execute(text("""
                CREATE TABLE m1_migration_preview_messages (
                    id VARCHAR(80) PRIMARY KEY,
                    session_id VARCHAR(80) NOT NULL
                      REFERENCES m1_migration_preview_sessions(id) ON DELETE CASCADE,
                    content TEXT NOT NULL
                )
            """))
            connection.execute(text("""
                INSERT INTO m1_migration_preview_sessions(id, owner, name, message_count)
                VALUES ('s1', 'alice', 'Existing session', 1),
                       ('s2', NULL, 'Legacy session', 0)
            """))
            connection.execute(text("""
                INSERT INTO m1_migration_preview_messages(id, session_id, content)
                VALUES ('msg-1', 's1', 'Preserve this message')
            """))
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS m1_migration_preview_messages"))
            connection.execute(text("DROP TABLE IF EXISTS m1_migration_preview_sessions"))
        engine.dispose()


def migrate_preview(connection, *, duplicate_epoch=False):
    """Illustrative PostgreSQL-only DDL, NOT an application migration script.

    Do not execute this helper against any real database. Production migration
    additionally requires live writer integration, verified backup, and a
    non-destructive migration of the full inherited Session schema.
    """
    connection.execute(text("""
        ALTER TABLE m1_migration_preview_sessions
        ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
    """))
    connection.execute(text("""
        ALTER TABLE m1_migration_preview_sessions
        ADD COLUMN incarnation_id VARCHAR(32)
    """))
    ids = connection.execute(text(
        "SELECT id FROM m1_migration_preview_sessions ORDER BY id"
    )).scalars().all()
    shared = uuid4().hex
    for sid in ids:
        connection.execute(text("""
            UPDATE m1_migration_preview_sessions
            SET incarnation_id = :epoch WHERE id = :sid
        """), {"epoch": shared if duplicate_epoch else uuid4().hex, "sid": sid})
    connection.execute(text("""
        ALTER TABLE m1_migration_preview_sessions
        ALTER COLUMN incarnation_id SET NOT NULL
    """))
    connection.execute(text("""
        CREATE UNIQUE INDEX m1_migration_preview_epoch_unique
        ON m1_migration_preview_sessions (incarnation_id)
    """))
    connection.execute(text("""
        ALTER TABLE m1_migration_preview_sessions
        ADD CONSTRAINT m1_preview_revision_nonnegative CHECK (revision >= 0)
    """))


def test_postgres17_backfill_preserves_legacy_messages_and_owners(pg_legacy):
    with pg_legacy.begin() as connection:
        migrate_preview(connection)
        sessions = connection.execute(text("""
            SELECT id, owner, name, message_count, revision, incarnation_id
            FROM m1_migration_preview_sessions ORDER BY id
        """)).all()
        assert len(sessions) == 2
        assert sessions[0][:5] == (
            "s1", "alice", "Existing session", 1, 0,
        )
        assert sessions[1][:5] == (
            "s2", None, "Legacy session", 0, 0,
        )
        assert sessions[0][5] != sessions[1][5]
        message = connection.execute(text(
            "SELECT content FROM m1_migration_preview_messages WHERE id = 'msg-1'"
        )).scalar_one()
        assert message == "Preserve this message"

    with pg_legacy.connect() as connection:
        constraints = connection.execute(text("""
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'm1_migration_preview_sessions'
              AND column_name IN ('revision', 'incarnation_id')
        """)).all()
        assert set(constraints) == {
            ("revision", "NO"), ("incarnation_id", "NO"),
        }

    with pg_legacy.begin() as connection:
        updated = connection.execute(text("""
            UPDATE m1_migration_preview_sessions
            SET revision = revision + 1, name = 'Updated'
            WHERE id = 's1' AND owner = 'alice' AND revision = 0
        """))
        assert updated.rowcount == 1
        stale = connection.execute(text("""
            UPDATE m1_migration_preview_sessions
            SET revision = revision + 1, name = 'Stale'
            WHERE id = 's1' AND owner = 'alice' AND revision = 0
        """))
        assert stale.rowcount == 0
        assert connection.execute(text(
            "SELECT name FROM m1_migration_preview_sessions WHERE id='s1'"
        )).scalar_one() == "Updated"


def test_postgres17_duplicate_backfill_rejected_and_ddl_rolled_back(pg_legacy):
    with pytest.raises(IntegrityError):
        with pg_legacy.begin() as connection:
            migrate_preview(connection, duplicate_epoch=True)
    # PostgreSQL transactional DDL rolls back all schema changes on error.
    with pg_legacy.connect() as connection:
        columns = connection.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'm1_migration_preview_sessions'
        """)).scalars().all()
        assert "revision" not in columns
        assert "incarnation_id" not in columns
        assert connection.execute(text(
            "SELECT COUNT(*) FROM m1_migration_preview_messages"
        )).scalar_one() == 1
