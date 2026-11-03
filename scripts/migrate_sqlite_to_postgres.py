#!/usr/bin/env python3
"""Transactional SQLite -> PostgreSQL 17 migration of the Mnerys main database.

This is an explicit, offline administration tool. It NEVER changes the live
DATABASE_URL, source SQLite data, auxiliary SQLite databases or the systemd
service. Target must be a dedicated, entirely empty PostgreSQL 17 database.

Use MNERYS_MIGRATION_DATABASE_URL for the confidential target URL; never pass
database credentials as command-line arguments or publish this value.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from core.database import Base


class MigrationError(RuntimeError):
    pass


def verify_target_url(raw: str) -> str:
    if not raw:
        raise MigrationError("MNERYS_MIGRATION_DATABASE_URL is required")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise MigrationError("Invalid PostgreSQL connection URL") from exc
    if url.get_backend_name() != "postgresql" or not url.database:
        raise MigrationError("Target must be a dedicated PostgreSQL database")
    return raw


def snapshot_source(source: Path, destination: Path):
    """Use SQLite's online backup API; never open the live DB read/write."""
    if not source.is_file() or source.is_symlink():
        raise MigrationError("Source must be an existing regular SQLite database")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst, pages=128, sleep=0.05)
    destination.chmod(0o600)


def schema_check(source_conn, metadata):
    """Reject unknown/missing columns and tables rather than silently losing data."""
    source_schema = inspect(source_conn)
    expected = set(metadata.tables)
    present = set(source_schema.get_table_names())
    extra = {
        name for name in present - expected
        if not name.startswith("sqlite_") and not name.startswith("chat_messages_fts")
    }
    missing = expected - present
    if extra or missing:
        raise MigrationError("SQLite schema is not compatible with mapped Mnerys tables; "
                             "upgrade/inspect the source before migration")
    for table in metadata.sorted_tables:
        actual = {column["name"] for column in source_schema.get_columns(table.name)}
        required = set(table.columns.keys())
        if required != actual:
            raise MigrationError(
                f"SQLite table {table.name} has incompatible columns; "
                "migration requires a reviewed schema update"
            )


def row_digest(conn, table):
    """Verify logical row values, not PostgreSQL/SQLite physical encodings."""
    digest = hashlib.sha256()
    count = 0
    order = [col for col in table.primary_key.columns]
    stmt = select(table).order_by(*order) if order else select(table)
    result = conn.execute(stmt)
    for row in result.mappings():
        serializable = {}
        for key, value in row.items():
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            elif isinstance(value, bytes):
                value = {"binary_sha256": hashlib.sha256(value).hexdigest()}
            serializable[key] = value
        payload = json.dumps(
            serializable, ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return count, digest.hexdigest()


def migrate(source: Path, target_url: str, *, apply: bool, writer_stopped: bool = False):
    verify_target_url(target_url)
    if apply and not writer_stopped:
        raise MigrationError("Before --apply, stop the sole application writer "
                             "and explicitly pass --writer-stopped")
    if "ODYSSEUS_DATA_DIR" not in os.environ:
        raise MigrationError("Explicit ODYSSEUS_DATA_DIR is required to preserve encryption keys")
    data_dir = Path(os.environ["ODYSSEUS_DATA_DIR"]).expanduser().resolve()
    if not data_dir.is_dir():
        raise MigrationError("Configured data directory does not exist")
    source = source.expanduser()
    if source.is_symlink():
        raise MigrationError("SQLite source must not be a symlink")
    source = source.resolve()
    if not source.is_relative_to(data_dir):
        raise MigrationError("SQLite source must be under the configured data directory")

    with tempfile.TemporaryDirectory(prefix="mnerys-db-migrate-") as tempdir:
        temp = Path(tempdir)
        temp.chmod(0o700)
        snap = temp / "snapshot.db"
        try:
            snapshot_source(source, snap)
        except (sqlite3.Error, OSError) as exc:
            raise MigrationError("SQLite snapshot could not be read safely") from exc

        source_engine = create_engine(
            "sqlite:///" + str(snap),
            connect_args={"check_same_thread": False},
        )
        target_engine = create_engine(
            target_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10},
        )
        try:
            with target_engine.connect() as probe:
                version = int(probe.exec_driver_sql("SHOW server_version_num").scalar_one())
                if not 170000 <= version < 180000:
                    raise MigrationError("This migration tool has been validated for PostgreSQL 17 only")
                if inspect(probe).get_table_names():
                    raise MigrationError("PostgreSQL target is not empty; refusing overwrite")

            with source_engine.connect() as source_conn:
                schema_check(source_conn, Base.metadata)
                counts = {}
                for table in Base.metadata.sorted_tables:
                    counts[table.name] = int(
                        source_conn.execute(select(func.count()).select_from(table)).scalar_one()
                    )

                if not apply:
                    return {
                        "result": "validated",
                        "backend": "PostgreSQL 17",
                        "tables": len(counts),
                        "rows": sum(counts.values()),
                        "source_unchanged": True,
                        "target_unchanged": True,
                    }

                # DDL and all rows are one PostgreSQL transaction: on any
                # failure the target remains empty. SQLite snapshot is immutable.
                with target_engine.begin() as destination:
                    if inspect(destination).get_table_names():
                        raise MigrationError("PostgreSQL target changed after preflight")
                    Base.metadata.create_all(bind=destination)
                    for table in Base.metadata.sorted_tables:
                        result = source_conn.execute(select(table))
                        while batch := result.mappings().fetchmany(300):
                            destination.execute(table.insert(), [dict(row) for row in batch])

                    for table in Base.metadata.sorted_tables:
                        source_count, source_hash = row_digest(source_conn, table)
                        dest_count, dest_hash = row_digest(destination, table)
                        if (source_count, source_hash) != (dest_count, dest_hash):
                            raise MigrationError(
                                f"Row integrity check failed for table {table.name}; "
                                "transaction rolled back"
                            )
                return {
                    "result": "migrated",
                    "backend": "PostgreSQL 17",
                    "tables": len(counts),
                    "rows_verified": sum(counts.values()),
                    "source_unchanged": True,
                    "destination_transaction_committed": True,
                    "application_configuration_unchanged": True,
                }
        except SQLAlchemyError as exc:
            raise MigrationError(
                "Database migration failed; original SQLite state preserved. "
                "Inspect private PostgreSQL logs"
            ) from exc
        finally:
            source_engine.dispose()
            target_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Migrate the Mnerys primary SQLite DB to PostgreSQL 17")
    parser.add_argument("--sqlite", required=True, type=Path, help="Path to source app.db (not printed)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Read-only validation")
    mode.add_argument("--apply", action="store_true", help="Copy into an empty PostgreSQL 17 database")
    parser.add_argument("--writer-stopped", action="store_true",
                        help="Confirm sole writer stopped and SQLite was backed up")
    args = parser.parse_args()
    try:
        report = migrate(
            args.sqlite, os.environ.get("MNERYS_MIGRATION_DATABASE_URL", ""),
            apply=args.apply, writer_stopped=args.writer_stopped,
        )
    except MigrationError as exc:
        print("MIGRATION BLOCKED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
