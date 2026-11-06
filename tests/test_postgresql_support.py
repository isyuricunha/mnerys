"""PostgreSQL 17 compatibility tests; no production database is ever accessed.

The optional live test requires a disposable database whose name starts with
mnerys_test_ and which listens only on a high localhost test port.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sqlite_remains_default_and_options_are_dialect_specific():
    from core.database import _default_database_url, _database_engine_options
    assert _default_database_url().startswith("sqlite:///")
    assert "check_same_thread" in _database_engine_options("sqlite:///tmp/test.db")["connect_args"]
    assert _database_engine_options(
        "postgresql+psycopg2://user:<REDACTED>@db.example.com/test"
    ) == {"pool_pre_ping": True, "connect_args": {"connect_timeout": 10}}
    with pytest.raises(RuntimeError, match="SQLite and PostgreSQL"):
        _database_engine_options("mysql://user:<REDACTED>@db.example.com/test")


def test_init_db_dispatches_postgres_without_legacy_sqlite_migrations(monkeypatch):
    import core.database as database

    class StubEngine:
        class dialect:
            name = "postgresql"

    calls = []
    monkeypatch.setattr(database, "engine", StubEngine())
    monkeypatch.setattr(database, "_init_postgresql_database", lambda: calls.append("postgres"))
    monkeypatch.setattr(database, "_migrate_model_endpoints",
                        lambda: pytest.fail("SQLite migration was invoked"))
    database.init_db()
    assert calls == ["postgres"]


def test_postgresql_initializer_fails_closed_if_schema_missing_columns(monkeypatch):
    import core.database as database

    # The migration scripts set this env var for their own import windows;
    # the leak would silently bypass the fail-closed branch this test guards.
    monkeypatch.delenv("MNERYS_ALLOW_STARTUP_SCHEMA_MIGRATION", raising=False)

    class StubEngine:
        class dialect:
            name = "postgresql"

        def begin(self):
            class Connection:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

            return Connection()

    class StubInspector:
        @staticmethod
        def has_table(*args, **kwargs):
            return True

        @staticmethod
        def get_columns(*args, **kwargs):
            return []

    monkeypatch.setattr(database, "engine", StubEngine())
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda **kwargs: None)
    monkeypatch.setattr(database, "inspect", lambda conn: StubInspector())
    monkeypatch.setattr(database, "_migrate_email_account_default_invariant",
                        lambda: pytest.fail("Must fail before touching data"))
    with pytest.raises(RuntimeError, match="requires reviewed migrations before startup"):
        database._init_postgresql_database()


def test_postgresql_initializer_rejects_sqlite(monkeypatch):
    import core.database as database

    class StubEngine:
        class dialect:
            name = "sqlite"

    monkeypatch.setattr(database, "engine", StubEngine())
    with pytest.raises(RuntimeError, match="requires a PostgreSQL engine"):
        database._init_postgresql_database()


def test_postgresql_17_fresh_database_with_crud_and_constraints(tmp_path):
    """Child process isolates the DB URL before core.database is imported."""
    url = os.environ.get("MNERYS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("No disposable PostgreSQL 17 test database configured")

    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "postgresql"
        or not parsed.database
        or not parsed.database.startswith("mnerys_test_")
        or parsed.host != "localhost"
        or not 1024 <= int(parsed.port or 0) <= 65535
    ):
        pytest.fail("Refusing non-disposable/non-local PostgreSQL test URL")

    script = """
from core.database import Base, engine, init_db, SessionLocal, Session, ChatMessage
from sqlalchemy import inspect, text
assert engine.dialect.name == 'postgresql'
with engine.connect() as conn:
    assert conn.exec_driver_sql('SHOW server_version_num').scalar().startswith('17')
init_db()
init_db()
inspection = inspect(engine)
assert set(Base.metadata.tables).issubset(set(inspection.get_table_names()))
idx = inspection.get_indexes('email_accounts')
assert any(row['name'] == 'ux_email_accounts_one_default_per_owner'
           and row['unique'] for row in idx)
with SessionLocal() as db:
    db.add(Session(id='pg17-check', name='PG17 check',
                   endpoint_url='https://example.com/v1', model='test-model'))
    db.add(ChatMessage(id='pg17-check-message', session_id='pg17-check',
                       role='user', content='Hello PostgreSQL'))
    db.commit()
    assert db.query(ChatMessage).filter_by(id='pg17-check-message').one().content == 'Hello PostgreSQL'
    assert db.query(Session).filter_by(id='pg17-check').one().name == 'PG17 check'
    db.delete(db.query(Session).filter_by(id='pg17-check').one())
    db.commit()
    assert db.query(ChatMessage).filter_by(id='pg17-check-message').count() == 0
engine.dispose()
"""
    env = {**os.environ, "DATABASE_URL": url, "ODYSSEUS_DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
    )
    error_text = (result.stdout[-400:] + result.stderr[-900:]).replace(url, "<REDACTED_URL>")
    if parsed.password:
        error_text = error_text.replace(parsed.password, "<REDACTED>")
    assert result.returncode == 0, "Disposable PostgreSQL 17 test failed: " + error_text
