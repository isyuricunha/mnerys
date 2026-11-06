#!/usr/bin/env python3
"""Explicit, non-destructive M2.2 session write-fencing migration.

Adds the additive `sessions.revision`, `sessions.incarnation_id` and
`background_tool_jobs.origin_incarnation_id` columns and backfills existing
session rows with revision 0 and a fresh incarnation. The default is
read-only. --apply is refused without an independently tested backup/restore,
operator consent, and an explicit DATABASE_URL. This installer never migrates
user messages, touches other tables, or prints connection details. Do not run
against live production without the separately authorized migration plan
(docs/architecture/SESSION_REVISION_MIGRATION.md).
"""
from __future__ import annotations

import argparse
import os
import uuid

from sqlalchemy import inspect, text

_SESSION_COLUMNS = (
    ("revision", "INTEGER"),
    ("incarnation_id", "VARCHAR(64)"),
)
_JOB_COLUMNS = (
    ("origin_incarnation_id", "VARCHAR(64)"),
)


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def inspect_or_install(engine, *, apply: bool = False) -> str:
    """Report or apply the additive fencing columns with per-row backfill."""
    session_columns = _columns(engine, "sessions")
    job_columns = _columns(engine, "background_tool_jobs")
    if not session_columns:
        raise RuntimeError("sessions table is missing")
    if not job_columns:
        raise RuntimeError("background_tool_jobs table is missing")

    # Adapter-resolved state report
    have = {
        "sessions": session_columns,
        "jobs": job_columns,
    }
    needed = {
        "sessions": {
            name for name, _suffix in _SESSION_COLUMNS
        } - have["sessions"],
        "jobs": {
            name for name, _suffix in _JOB_COLUMNS
        } - have["jobs"],
    }
    if not needed["sessions"] and not needed["jobs"]:
        # Idempotent recheck: existing rows must be backfilled, otherwise the
        # guarded writers fail closed. Never repair silently.
        with engine.connect() as conn:
            unfilled = conn.execute(text(
                "select count(*) from sessions "
                "where revision is null or incarnation_id is null"
            )).scalar()
        if unfilled:
            return f"needs_backfill:{unfilled}"
        return "already_installed"
    if not apply:
        return "missing"

    with engine.begin() as conn:
        for column, suffix in _SESSION_COLUMNS:
            if column in needed["sessions"]:
                conn.execute(text(
                    f"ALTER TABLE sessions ADD COLUMN {column} {suffix}"
                ))
        for column, suffix in _JOB_COLUMNS:
            if column in needed["jobs"]:
                conn.execute(text(
                    f"ALTER TABLE background_tool_jobs ADD COLUMN {column} {suffix}"
                ))
        rows = conn.execute(text("select id from sessions")).fetchall()
        for (session_id,) in rows:
            conn.execute(text(
                "update sessions set revision = 0, incarnation_id = :incarnation "
                "where id = :session_id and revision is null "
                "and incarnation_id is null"
            ), {"incarnation": uuid.uuid4().hex, "session_id": session_id})
    return "installed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-backup", action="store_true")
    args = parser.parse_args()
    if args.check and args.apply:
        parser.error("Choose --check or --apply")
    if not os.environ.get("DATABASE_URL"):
        print("Refusing fencing migration check: explicit DATABASE_URL required")
        return 3
    if args.apply and (
        not args.verified_backup
        or os.environ.get("MNERYS_SESSION_FENCING_MIGRATION_APPROVED") != "1"
    ):
        print("Refusing fencing migration: reviewed backup/restore and approval required")
        return 3

    # The explicit, operator-authorized migration is the only valid caller
    # of the startup schema escape; the application never sets it.
    os.environ["MNERYS_ALLOW_STARTUP_SCHEMA_MIGRATION"] = "1"

    from core.database import engine
    try:
        result = inspect_or_install(engine, apply=args.apply)
    except Exception:
        print("Session fencing schema verification/installation failed")
        return 2
    print("Session write fencing schema:", result)
    return 0 if not str(result).startswith(("missing", "needs_backfill")) else 3


if __name__ == "__main__":
    raise SystemExit(main())
