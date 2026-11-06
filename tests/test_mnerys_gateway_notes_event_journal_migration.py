"""Explicit journal installation requires schema and backup authorization."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from scripts.migrations.notes_event_journal_v1 import inspect_or_install
from src.gateway_notes_decision_fence import notes_fence_metadata
from src.gateway_notes_event_journal import NotesEventJournal, notes_event_journal_metadata
from tests.test_mnerys_gateway_notes_decision_fence import engine as isolated_fence_engine


def test_operator_migration_is_non_destructive_and_idempotent(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "journal.db"))
    try:
        notes_fence_metadata().create_all(engine)
        assert inspect_or_install(engine, apply=False) == "missing"
        assert not inspect(engine).has_table("gateway_notes_event_journal")
        assert inspect_or_install(engine, apply=True) == "installed"
        assert inspect_or_install(engine, apply=True) == "already_installed"
        journal = NotesEventJournal(engine)
        event_id = journal.append(
            owner="alice", session_id="session-1",
            origin_user_receipt="user-01", approval_id="A" * 43,
            kind="review_registered", now=1_800_000_000.0,
        )
        assert inspect_or_install(engine, apply=True) == "already_installed"
        assert journal.replay(
            owner="alice", session_id="session-1",
            origin_user_receipt="user-01",
        )[0].cursor == event_id
    finally:
        engine.dispose()


def test_missing_fence_or_partial_journal_schema_fails_closed(tmp_path):
    db = create_engine("sqlite+pysqlite:///" + str(tmp_path / "partial.db"))
    try:
        with pytest.raises(RuntimeError):
            inspect_or_install(db, apply=False)
        notes_fence_metadata().create_all(db)
        with db.begin() as connection:
            connection.execute(text(
                "CREATE TABLE gateway_notes_event_journal "
                "(event_id INTEGER PRIMARY KEY)"
            ))
        with pytest.raises(RuntimeError, match="Incompatible"):
            inspect_or_install(db, apply=False)
        with pytest.raises(RuntimeError, match="Incompatible"):
            inspect_or_install(db, apply=True)
        assert {c["name"] for c in inspect(db).get_columns(
            "gateway_notes_event_journal"
        )} == {"event_id"}
    finally:
        db.dispose()


def test_cli_never_applies_without_selected_database_and_verified_approval(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts/migrations/notes_event_journal_v1.py"
    db_file = tmp_path / "intended.db"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(repo),
        "DATABASE_URL": "sqlite+pysqlite:///" + str(db_file),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for args in (
        ["--apply"],
        ["--apply", "--verified-backup"],
    ):
        done = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=repo, env=env, text=True, capture_output=True,
            timeout=15,
        )
        assert done.returncode == 3
        assert "Refusing journal migration" in done.stdout
        assert not db_file.exists()
    env.pop("DATABASE_URL")
    done = subprocess.run(
        [sys.executable, str(script), "--check"], cwd=repo,
        env=env, text=True, capture_output=True, timeout=15,
    )
    assert done.returncode == 3
    assert "explicit DATABASE_URL" in done.stdout


def test_explicit_installation_and_reopen_on_isolated_backends(isolated_fence_engine):
    """Exercise the operator migration on SQLite and disposable PostgreSQL 17."""
    db = isolated_fence_engine
    table_name = "gateway_notes_event_journal"
    assert not inspect(db).has_table(table_name)
    assert inspect_or_install(db, apply=False) == "missing"
    assert not inspect(db).has_table(table_name)
    try:
        assert inspect_or_install(db, apply=True) == "installed"
        assert inspect_or_install(db, apply=False) == "already_installed"
        journal = NotesEventJournal(db)
        cursor = journal.append(
            owner="alice", session_id="session-1",
            origin_user_receipt="user-01", approval_id="A" * 43,
            kind="review_registered", now=1_800_000_000.0,
        )
        # A fresh Engine/connection represents a new worker after restart.
        # A memory-only SQLite database has no independently reopenable state.
        if db.dialect.name == "sqlite" and db.url.database in (None, ":memory:"):
            reopened = db
        else:
            reopened = create_engine(db.url, pool_pre_ping=True)
        try:
            recovered = NotesEventJournal(reopened)
            recovered.require_schema()
            assert [event.cursor for event in recovered.replay(
                owner="alice", session_id="session-1",
                origin_user_receipt="user-01",
            )] == [cursor]
            assert inspect_or_install(reopened, apply=True) == "already_installed"
        finally:
            if reopened is not db:
                reopened.dispose()
    finally:
        notes_event_journal_metadata().drop_all(db)
