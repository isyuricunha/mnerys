# M2 private preview rollout plan (prepared; execution requires authorization)

This is the exact, pre-approved-or-not release and migration scope for
moving the private development preview to the published M2 revision
(v0.10.0). **Nothing here has been executed.** The preview keeps running
the previous release until Yuri explicitly authorizes the rollout.

## Scope

- Code: the published `main` snapshot at `f33902bb` (semantic-release
  v0.10.0), or any later authorized revision.
- Two additive schema migrations, in order, with the native preview's
  PostgreSQL 17 database:
  1. `scripts/migrations/session_revision_fencing_v1.py` (sessions
     fencing columns + backfill).
  2. `scripts/migrations/gateway_turn_records_v1.py` (durable
     turn/event/approval tables).
- Feature switches stay **off** after rollout: the Web turn continues on
  the legacy chain. `MNERYS_M21_WEB_CORE_CUTOVER=1` and durable recording
  are enabled only by separate explicit operator action.

## Order of operations

1. **Backup.** Take a private PostgreSQL 17 custom-format archive with the
   preview's verified backup helper and confirm the restore tooling
   accepts it (the drill already validated this procedure on disposable
   data: backup, drop, restore, schema/backfill intact).
2. **Readiness.** Confirm the current service health and readiness checks
   pass, and record the current release directory and service unit state
   for rollback.
3. **Stop/brief interruption.** A short controlled stop is acceptable per
   the preview policy.
4. **Migrate** with the approval flags and `--verified-backup`; both
   migrations are read-only by default and refuse `--apply` without the
   flags. Verify the reported `already_installed`/`installed` and backfill
   state.
5. **Install the new build** through the native runner (systemd/uvicorn),
   which validates the candidate and restarts.
6. **Verify.**
   - `GET /api/ready` (authenticated) and the trusted local database
     readiness probe.
   - One chat turn via the existing Web UI (legacy path, unchanged).
   - The v1 Gateway router responds to an authenticated bearer request
     (`/api/v1/models` and `/api/v1/sessions`) without granting anything.
   - Desktop and mobile browser checks of the main chat flow.
   - The fencing columns are visible in the primary database and the
     durable tables exist.

## Rollback

- Code rollback: the runner's verified rollback to the previous healthy
  release keeps user data intact; the additive schema does not break a
  pre-M2 build (extra columns/tables are ignored).
- Database rollback: only the restored backup archive, never a blind
  reverse migration. If anything is ambiguous after a failed rollout,
  stop, preserve the archive, and investigate before state restoration.
- No feature switch (cutover, durable recording, approvals records) is
  enabled by this rollout, so operational behavior is unchanged until
  each switch is turned on deliberately with its own validation.

## Risks and compatibility

- The pre-M2 build and the M2 build differ in the session writers only
  for migrated rows; unmigrated behavior is preserved until backfill.
- The startup guard fails closed on PostgreSQL 17 until both migrations
  are installed: the new build must not start before the migration step
  completes.
- No provider credentials, private addresses or production data are used
  in this plan's validation steps; the plan is executable from the
  preview host with the existing environment only.