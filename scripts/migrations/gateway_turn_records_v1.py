#!/usr/bin/env python3
"""Explicit M2.3 durable turn/event/approval record installation.

Creates the three additive Gateway record tables (gateway_turns,
gateway_turn_events, gateway_approvals). Read-only by default; --apply is
refused without a verified backup/restore, operator consent and an explicit
DATABASE_URL. Never auto-installed by application startup.
"""
from __future__ import annotations

import argparse
import os

from sqlalchemy import inspect

from core.database import (
    Base, GatewayApproval, GatewayTurn, GatewayTurnEvent,
)

_TABLES = ("gateway_turns", "gateway_turn_events", "gateway_approvals")


def inspect_or_install(engine, *, apply: bool = False) -> str:
    """Report or create only the additive record tables."""
    inspector = inspect(engine)
    missing = [name for name in _TABLES if not inspector.has_table(name)]
    if not missing:
        return "already_installed"
    if not apply:
        return "missing"
    with engine.begin() as conn:
        Base.metadata.create_all(
            bind=conn, checkfirst=False, tables=[
                GatewayTurn.__table__, GatewayTurnEvent.__table__,
                GatewayApproval.__table__,
            ],
        )
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
        print("Refusing turn-record migration check: explicit DATABASE_URL required")
        return 3
    if args.apply and (
        not args.verified_backup
        or os.environ.get("MNERYS_TURN_RECORDS_MIGRATION_APPROVED") != "1"
    ):
        print("Refusing turn-record migration: reviewed backup/restore and approval required")
        return 3

    from core.database import engine
    try:
        result = inspect_or_install(engine, apply=args.apply)
    except Exception:  # noqa: BLE001  (operator-facing migration guard)
        print("Turn-record schema verification/installation failed")
        return 2
    print("Gateway turn records schema:", result)
    return 0 if result != "missing" else 3


if __name__ == "__main__":
    raise SystemExit(main())