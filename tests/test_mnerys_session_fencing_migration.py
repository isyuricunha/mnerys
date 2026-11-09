"""M2.2 session write-fencing migration: legacy-schema backfill acceptance.

Runs against a disposable file-backed SQLite database and the dedicated
disposable PostgreSQL 17 harness (MNERYS_M1_PG_TEST_DSN, loopback-only,
refused otherwise). No production database is touched.
"""

import os
import re
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.database as core_db  # noqa: E402
from scripts.migrations.session_revision_fencing_v1 import (  # noqa: E402
    inspect_or_install,
)

PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"

LEGACY_SESSIONS = (
    "create table sessions ("
    " id varchar primary key, owner varchar, name varchar not null,"
    " message_count integer default 0)"
)
LEGACY_JOBS = (
    "create table background_tool_jobs ("
    " id varchar primary key, session_id varchar, owner varchar,"
    " status varchar, message_id varchar)"
)


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def legacy_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
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
        # The PG17 drill reuses one disposable database across suites; drop
        # any previous suite's sessions/jobs tables before creating the
        # legacy schema. CASCADE is required because sibling suites create
        # dependent objects (views) over these tables in the same shared
        # disposable database.
        with engine.begin() as conn:
            # SQLite has no DROP ... CASCADE syntax; only the shared
            # PostgreSQL 17 database carries sibling suites' dependent views.
            if engine.dialect.name == "postgresql":
                conn.execute(text("DROP TABLE IF EXISTS background_tool_jobs CASCADE"))
                conn.execute(text("DROP TABLE IF EXISTS sessions CASCADE"))
            else:
                conn.execute(text("DROP TABLE IF EXISTS background_tool_jobs"))
                conn.execute(text("DROP TABLE IF EXISTS sessions"))
            conn.execute(text(LEGACY_SESSIONS))
            conn.execute(text(LEGACY_JOBS))
            conn.execute(text(
                "insert into sessions (id, owner, name) values"
                " ('s1', 'alice', 'First'), ('s2', 'bob', 'Second')"
            ))
            conn.execute(text(
                "insert into background_tool_jobs (id, session_id, owner, status)"
                " values ('j1', 's1', 'alice', 'running')"
            ))
        yield engine
    finally:
        engine.dispose()


def _columns(engine, table):
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_missing_columns_report_before_apply(legacy_engine):
    assert inspect_or_install(legacy_engine) == "missing"
    assert "revision" not in _columns(legacy_engine, "sessions")
    assert "incarnation_id" not in _columns(legacy_engine, "sessions")


def test_apply_backfills_every_legacy_row_exactly_once(legacy_engine):
    assert inspect_or_install(legacy_engine, apply=True) == "installed"
    columns = _columns(legacy_engine, "sessions")
    assert {"revision", "incarnation_id"} <= columns
    assert "origin_incarnation_id" in _columns(legacy_engine, "background_tool_jobs")
    with legacy_engine.connect() as conn:
        rows = conn.execute(text(
            "select id, revision, incarnation_id from sessions order by id"
        )).fetchall()
    assert [(row[0], row[1]) for row in rows] == [("s1", 0), ("s2", 0)]
    incarnations = [row[2] for row in rows]
    assert all(re.fullmatch(r"[0-9a-f]{32}", item) for item in incarnations)
    assert len(set(incarnations)) == len(incarnations)


def test_second_install_is_idempotent_and_backfills_late_rows(legacy_engine):
    assert inspect_or_install(legacy_engine, apply=True) == "installed"
    assert inspect_or_install(legacy_engine, apply=True) == "already_installed"
    with legacy_engine.begin() as conn:
        conn.execute(text("update sessions set incarnation_id = null where id = 's1'"))
    # A row written after the first apply (an unfenced writer that has since
    # been fixed) is repaired by the idempotent apply, never by the check.
    assert inspect_or_install(legacy_engine, apply=True) == "backfilled"
    assert inspect_or_install(legacy_engine, apply=True) == "already_installed"
    # A read-only check still reports the gap without repairing it.
    with legacy_engine.begin() as conn:
        conn.execute(text("update sessions set incarnation_id = null where id = 's1'"))
    assert inspect_or_install(legacy_engine) == "needs_backfill:1"


def test_migration_refuses_unapproved_apply(monkeypatch, tmp_path):
    env_file = tmp_path / "sqlite.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{env_file}")
    monkeypatch.delenv("MNERYS_SESSION_FENCING_MIGRATION_APPROVED", raising=False)
    import scripts.migrations.session_revision_fencing_v1 as migration

    monkeypatch.setattr(
        "sys.argv", ["migration", "--apply", "--verified-backup"],
    )
    assert migration.main() == 3
    monkeypatch.setenv("MNERYS_SESSION_FENCING_MIGRATION_APPROVED", "1")
    # Fully approved but pointed at an empty throwaway database with no
    # tables: the verification gate must refuse instead of creating anything.
    empty = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    monkeypatch.setattr(core_db, "engine", empty)
    assert migration.main() == 2
    empty.dispose()


def test_missing_database_url_refuses(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import scripts.migrations.session_revision_fencing_v1 as migration

    monkeypatch.setattr("sys.argv", ["migration", "--check"])
    assert migration.main() == 3
