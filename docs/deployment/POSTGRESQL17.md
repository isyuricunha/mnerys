# PostgreSQL 17 support — primary Mnerys database

**Status:** PostgreSQL 17 is a supported *optional backend for clean primary-database schemas*, validated against a disposable PostgreSQL 17 server. **SQLite remains the default** and is not deprecated.

## Scope and limitations

| Component | State |
| --- | --- |
| Main SQLAlchemy tables (sessions, documents, tasks, model endpoints and other mapped records) | PostgreSQL 17 supported for clean databases |
| Startup migrations | PostgreSQL has a dedicated path; existing incomplete schemas fail closed until a reviewed migration |
| Default installation | SQLite, unchanged |
| SQLite → PostgreSQL main-database data transfer | Explicit transactional migration tool; no automatic live cutover |
| Local email/scheduling auxiliary storage | Still SQLite; **not** moved by `DATABASE_URL` |
| Local auth/settings files, encrypted secrets key, uploads, vector/search state | Remain local; preserve `DATA_DIR` |
| Chat transcript search | SQLite FTS5 is SQLite-only; PostgreSQL uses the existing SQLAlchemy text-search fallback |
| Native preview deploy automation | **SQLite backups only** until PostgreSQL backup/rollback handling is implemented and validated |

A PostgreSQL connection does **not** magically replace local file storage or migrate email scheduling. No backend switching occurs automatically.

## New installation

Create a **dedicated database and least-privilege role** in your PostgreSQL 17 server. Keep credentials and the endpoint in private environment configuration, **never in Git, documentation, screenshots or shared logs**.

Use SQLAlchemy's `postgresql+psycopg2://` connection URL in the **private** `DATABASE_URL` setting. A generic illustration, not a real endpoint:

```dotenv
DATABASE_URL=postgresql+psycopg2://<DB_USER>:<DB_PASSWORD>@db.example.com/<DB_NAME>
```

The `psycopg2-binary` driver is already installed from `requirements.txt`. Use encrypted network transport, proper authentication and narrowly scoped database privileges appropriate to your environment. Provisioning PostgreSQL remains external to the Mnerys application; the app runs natively and does not require Docker.

On startup, SQLAlchemy creates missing tables. Mnerys then inspects the PostgreSQL schema and refuses to start if mapped columns are missing. This is deliberate: **`create_all()` does not upgrade existing tables**, and arbitrary automatic DDL changes can corrupt an established installation. PostgreSQL startup does not run the legacy SQLite migrations.

## Migrating an existing SQLite main database

**Do not point a live Mnerys instance at PostgreSQL and assume data will follow.** Migrate during a planned, approved maintenance operation, after validating backups and recovery.

The repo contains `scripts/migrate_sqlite_to_postgres.py`, which works on a **consistent snapshot** of the SQLite main database and copies only mapped SQLAlchemy tables into a **completely empty PostgreSQL 17 database**.

- The tool uses the SQLite online backup API, reads the source without modifying it and verifies its schema before transfer.
- Data and DDL are inserted in a single PostgreSQL transaction. After every table is copied, logical row counts and contents are compared; a failed copy rolls the destination transaction back.
- It rejects missing/extra source columns, nonempty targets, other PostgreSQL major versions, and `--apply` without an explicit **sole writer stopped** acknowledgment.
- It does not configure the application, change the live database, restart `systemd`, delete SQLite, change auth files or migrate auxiliary SQLite/Chroma stores.
- Ensure the original private `DATA_DIR`, including encryption key material, remains in place. Never check in or publish that key.

Use the **same private data directory** for the command and the deployment. The target URL is provided only via `MNERYS_MIGRATION_DATABASE_URL` in the **process environment**; do not supply it as a command-line argument. Avoid echoing or persisting it in shell history or shared logs.

```bash
# DB credentials/target URL and data directory are set through private host configuration.
# Inspect source schema and target; no PostgreSQL writes.
python3 -m scripts.migrate_sqlite_to_postgres \
  --sqlite "<PRIVATE_DATA_DIR>/app.db" --check

# Only after stopping the sole application writer, confirming a verified
# backup and approving the maintenance/cutover plan:
python3 -m scripts.migrate_sqlite_to_postgres \
  --sqlite "<PRIVATE_DATA_DIR>/app.db" --apply --writer-stopped
```

The source and destination may contain private user conversations, settings and tokens. **Do not expose migration dumps, query results, connection URLs or logs in any public artifact.**

**Before a real cutover:** configure a PostgreSQL 17 backup/restore strategy; test recovery into an isolated instance; plan how to freeze writes and scheduled jobs; verify application-level workflows and user ownership after migration; arrange an explicit, reversible configuration switch; and only then restart the live service. The **current native preview automation must not be used for PostgreSQL cutover** until its database-backup and rollback path is adapted.

## Test coverage

The optional integration tests in `tests/test_postgresql_support.py` and `tests/test_sqlite_postgres_migration.py` use a disposable PostgreSQL 17 database. They verify schema creation, repeat initialization, constraints, sessions/messages CRUD, transactionally copied data and safety blocks. A dedicated CI workflow provisions a temporary PostgreSQL 17 test service, with generated credentials, then destroys it.

Run the unit portion locally (PostgreSQL integration is skipped if no disposable URL is supplied):

```bash
python3 -m pytest -q tests/test_postgresql_support.py tests/test_sqlite_postgres_migration.py
```

The first real PostgreSQL 17 integration checks passed against a temporary database; **the owner's live PostgreSQL server was not accessed, configured or modified**.

## Official technical references

- [SQLAlchemy PostgreSQL dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)
- [SQLAlchemy schema reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html)
- [PostgreSQL 17 ALTER TABLE](https://www.postgresql.org/docs/17/sql-altertable.html)
- [Alembic migration design](https://alembic.sqlalchemy.org/en/latest/batch.html)

Long term, move handwritten schema migrations to a versioned, reviewable migration framework with PostgreSQL/SQLite dialect-specific operations. Do not replace safe migration plans with automatic schema guesses.
