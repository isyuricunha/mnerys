#!/usr/bin/env python3
"""Explicit, non-destructive notes_v1 decision-fence schema installation.

Default = read-only check. The script NEVER drops tables or rows, edits
existing tables, or changes the running Web feature flag.

An operator must back up and verify a restore of the actual target database
before running --apply. Do not place the database URL on a command line:
set DATABASE_URL through the operator-controlled environment instead.
"""
from __future__ import annotations

import argparse
import os

from sqlalchemy import inspect

from src.gateway_notes_decision_fence import (
    NotesDecisionFence, notes_fence_metadata,
)

_TABLE = "gateway_notes_decision_fences"


def inspect_or_install(engine, *, apply: bool = False) -> str:
    """Only create the independent v1 table when absent and explicitly approved."""
    fence = NotesDecisionFence(engine)
    if inspect(engine).has_table(_TABLE):
        fence.require_schema()
        return "already_installed"
    if not apply:
        return "missing"
    # PostgreSQL DDL is transactional. SQLite's table creation may not roll
    # back all DDL if interrupted, so NEVER invent a destructive rollback.
    # If a failure leaves an incomplete schema, require manual reconciliation.
    with engine.begin() as conn:
        notes_fence_metadata().create_all(bind=conn, checkfirst=False)
    fence.require_schema()
    return "installed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true",
        help="Read-only validation (also the default)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Install the new v1 table ONLY, without modifying existing tables",
    )
    parser.add_argument(
        "--verified-backup", action="store_true",
        help="Confirm a tested backup/restore and approved migration plan",
    )
    options = parser.parse_args()
    if options.check and options.apply:
        parser.error("Choose either --check or --apply")
    # A missing DATABASE_URL could otherwise initialize the application's
    # default SQLite path, unrelated to the operator's intended target.
    if not os.environ.get("DATABASE_URL"):
        print("Refusing schema check: an explicit DATABASE_URL is required")
        return 3
    if options.apply and (
        not options.verified_backup
        or os.environ.get("MNERYS_NOTES_FENCE_MIGRATION_APPROVED") != "1"
    ):
        print("Refusing migration: verified backup and explicit approval required")
        return 3

    from core.database import engine
    try:
        state = inspect_or_install(engine, apply=options.apply)
    except Exception:
        # Do not print connection strings or SQL exceptions: provider/database
        # messages may include private hostnames, schema or credentials.
        print("Notes decision fence schema inspection/installation failed")
        return 2
    print("Notes decision fence schema:", state)
    return 0 if state != "missing" else 3


if __name__ == "__main__":
    raise SystemExit(main())
