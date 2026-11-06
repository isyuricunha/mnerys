"""Migration preview: test-only SQL engines; never configured DBs."""

from pathlib import Path
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text
import pytest

from scripts.migrations.notes_decision_fence_v1 import inspect_or_install
from src.gateway_notes_decision_fence import NotesDecisionFence


def test_explicit_migration_can_install_and_recheck_without_data_loss(tmp_path):
    db = create_engine("sqlite+pysqlite:///" + str(tmp_path / "install.db"))
    try:
        assert inspect_or_install(db, apply=False) == "missing"
        assert not inspect(db).has_table("gateway_notes_decision_fences")
        assert inspect_or_install(db, apply=True) == "installed"
        assert inspect_or_install(db, apply=False) == "already_installed"
        assert inspect_or_install(db, apply=True) == "already_installed"
        fence = NotesDecisionFence(db)
        fence.require_schema()
        now = 1_800_000_000.0
        assert fence.issue(
            owner="alice", session_id="session-1",
            approval_id="A" * 43,
            action_digest="a" * 64,
            origin_user_receipt="user-1",
            issued_at=now - 1, expires_at=now + 300,
            now=now,
        )
        assert inspect_or_install(db, apply=True) == "already_installed"
        assert fence.status(
            owner="alice", session_id="session-1",
            approval_id="A" * 43, now=now,
        ).state == "pending"
    finally:
        db.dispose()


def test_partial_existing_schema_fails_closed_without_automatic_repair(tmp_path):
    db = create_engine("sqlite+pysqlite:///" + str(tmp_path / "partial.db"))
    try:
        with db.begin() as conn:
            conn.execute(text(
                "CREATE TABLE gateway_notes_decision_fences "
                "(approval_id VARCHAR(43) PRIMARY KEY)"
            ))
        with pytest.raises(RuntimeError, match="incompatible"):
            inspect_or_install(db, apply=False)
        with pytest.raises(RuntimeError, match="incompatible"):
            inspect_or_install(db, apply=True)
        cols = {
            item["name"]
            for item in inspect(db).get_columns("gateway_notes_decision_fences")
        }
        assert cols == {"approval_id"}
    finally:
        db.dispose()


def test_cli_requires_explicit_database_and_reviewed_backup(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] /
        "scripts/migrations/notes_decision_fence_v1.py"
    )
    scratch_db = tmp_path / "target.db"
    # No secrets or services from the developer environment are forwarded.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "DATABASE_URL": "sqlite+pysqlite:///" + str(scratch_db),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for args in [
        ["--apply"],
        ["--apply", "--verified-backup"],
    ]:
        denied = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=15, check=False,
        )
        assert denied.returncode == 3
        assert "Refusing migration" in denied.stdout
        assert not scratch_db.exists()

    no_db_env = dict(env)
    no_db_env.pop("DATABASE_URL")
    denied = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=tmp_path, env=no_db_env, capture_output=True, text=True,
        timeout=15, check=False,
    )
    assert denied.returncode == 3
    assert "explicit DATABASE_URL" in denied.stdout
