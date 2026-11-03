"""Safety and PostgreSQL 17 integration coverage for SQLite main-DB migration."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url

from scripts.migrate_sqlite_to_postgres import (
    MigrationError,
    migrate,
    verify_target_url,
)


def test_migration_never_accepts_non_postgresql_targets():
    with pytest.raises(MigrationError, match="PostgreSQL"):
        verify_target_url("sqlite:///example.db")
    with pytest.raises(MigrationError, match="required"):
        verify_target_url("")


def test_migration_requires_explicit_writer_shutdown(tmp_path):
    with pytest.raises(MigrationError, match="sole application writer"):
        migrate(tmp_path / "app.db",
                "postgresql+psycopg2://tester:<REDACTED>@db.example.com/mnerys_test_example",
                apply=True)


def test_migration_without_data_dir_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_DATA_DIR", raising=False)
    with pytest.raises(MigrationError, match="ODYSSEUS_DATA_DIR"):
        migrate(tmp_path / "app.db",
                "postgresql+psycopg2://tester:<REDACTED>@db.example.com/mnerys_test_example",
                apply=False)


def test_migrate_synthetic_sqlite_to_pg17(tmp_path, monkeypatch):
    url = os.environ.get("MNERYS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL 17 database")
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "postgresql"
        or not parsed.database
        or not parsed.database.startswith("mnerys_test_")
        or parsed.host != "localhost"
        or not 1024 <= int(parsed.port or 0) <= 65535
    ):
        pytest.fail("Refusing non-isolated PostgreSQL migration target")

    from core.database import Base, ChatMessage, Session
    private_dir = tmp_path / "data"
    private_dir.mkdir()
    sqlite_file = private_dir / "app.db"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(private_dir))
    source_engine = create_engine("sqlite:///" + str(sqlite_file))
    Base.metadata.create_all(source_engine)
    with source_engine.begin() as conn:
        conn.execute(
            Session.__table__.insert(),
            {
                "id": "synthetic-session",
                "name": "Example session",
                "endpoint_url": "https://example.com/v1",
                "model": "test",
                "owner": "tester",
            },
        )
        conn.execute(
            ChatMessage.__table__.insert(),
            {
                "id": "synthetic-message",
                "session_id": "synthetic-session",
                "role": "user",
                "content": "Only a synthetic test record",
            },
        )
    source_engine.dispose()

    check = migrate(sqlite_file, url, apply=False)
    assert check["result"] == "validated"
    assert check["target_unchanged"] is True
    assert check["rows"] >= 2

    verified = migrate(sqlite_file, url, apply=True, writer_stopped=True)
    assert verified["result"] == "migrated"
    assert verified["source_unchanged"] is True
    assert verified["rows_verified"] == check["rows"]

    postgres = create_engine(url)
    with postgres.connect() as conn:
        assert inspect(conn).has_table("sessions")
        saved = conn.execute(select(Session.__table__).where(
            Session.__table__.c.id == "synthetic-session"
        )).mappings().one()
        assert saved["owner"] == "tester"
        message = conn.execute(select(ChatMessage.__table__).where(
            ChatMessage.__table__.c.id == "synthetic-message"
        )).mappings().one()
        assert message["content"] == "Only a synthetic test record"
    postgres.dispose()
