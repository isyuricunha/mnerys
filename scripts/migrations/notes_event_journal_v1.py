#!/usr/bin/env python3
"""Explicit, non-destructive notes_v1 metadata event-journal installation.

The default is read-only. --apply is refused without an independently tested
backup/restore, operator consent, and explicit DATABASE_URL. This installer
never enables the notes Web adapter, migrates user messages, prunes records,
or prints connection errors/credentials. Do not run against live production
without the separately authorized migration plan.
"""
from __future__ import annotations

import argparse
import os

from sqlalchemy import inspect

from src.gateway_notes_event_journal import (
    NotesEventJournal, notes_event_journal_metadata,
)
from src.gateway_notes_decision_fence import NotesDecisionFence

_TABLE = "gateway_notes_event_journal"


def inspect_or_install(engine, *, apply: bool = False) -> str:
    """Install only the new independent journal table, never repair silently."""
    NotesDecisionFence(engine).require_schema()
    journal = NotesEventJournal(engine)
    if inspect(engine).has_table(_TABLE):
        journal.require_schema()
        return "already_installed"
    if not apply:
        return "missing"
    with engine.begin() as conn:
        notes_event_journal_metadata().create_all(bind=conn, checkfirst=False)
    journal.require_schema()
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
        print("Refusing notes journal check: explicit DATABASE_URL required")
        return 3
    if args.apply and (
        not args.verified_backup
        or os.environ.get("MNERYS_NOTES_JOURNAL_MIGRATION_APPROVED") != "1"
    ):
        print("Refusing journal migration: reviewed backup/restore and approval required")
        return 3

    from core.database import engine
    try:
        result = inspect_or_install(engine, apply=args.apply)
    except Exception:
        print("Notes event journal schema verification/installation failed")
        return 2
    print("Notes event journal schema:", result)
    return 0 if result != "missing" else 3


if __name__ == "__main__":
    raise SystemExit(main())
