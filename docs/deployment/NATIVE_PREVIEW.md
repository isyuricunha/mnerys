# Mnerys — private native development preview

This page documents the verified native preview arrangement, not a Docker-based Mnerys app deployment.

## Runtime and operating status

The private Mnerys preview is running directly from a versioned Git source snapshot with its own Python 3.12 virtual environment and Uvicorn process. Two **user-level systemd services** are enabled for restart and automatic startup:

- Application unit — application, listening only on loopback.
- Gateway unit — Caddy proxy, bound **only to the host's Tailscale address**.

The actual unit names are stored privately on the host in the runtime configuration, not in public project files.

The edge gateway has **one fixed, randomly selected high TCP port**, configured privately. The public repository deliberately contains **no actual address, hostname, internal port, token or server path**. Consult your private host configuration to retrieve access details. The browser must be connected to the authorized tailnet.

Authentication is enabled and localhost auth bypass is disabled. The legacy Odysseus user-level application service was disabled only after the Mnerys native preview passed basic health, login and browser checks. Original Odysseus data files remain preserved separately.

The Mnerys application is **not containerized**. The maintainer's running native preview uses **PostgreSQL 17 for its primary SQLAlchemy database**, after an offline SQLite-to-PostgreSQL cutover validated against the real source records. SQLite remains the default for other installations. Auxiliary SQLite databases, authentication/settings files, and uploads remain in persistent storage. The previous SQLite primary database, validated backups, and prior release/service configuration are preserved for recovery. Existing optional dependencies (e.g. search/index services) may still run under their previously configured deployment method; do not disable them simply because the old Odysseus web service was retired.

## Data, versions and security

- Source releases are kept as immutable Git snapshots outside the checkout and outside persistent state.
- Runtime data is held outside the Git tree, with restrictive directory permissions. The original application's data was snapshotted for the Mnerys preview; it was **not deleted or moved**.
- Private environment values, backend bindings and gateway configuration live only in private host files. Do not commit them or paste their contents into issues or logs.
- The preview currently uses private **HTTP over encrypted Tailscale transport**. It must not be exposed to the public internet; publicly trusted HTTPS is a separate future deployment decision.
- The gateway port remains stable across application restarts and is deliberately not a random port per launch.
- Original code attribution and software/mascot licenses are independent of this deployment.

## Validation

The application exposes unauthenticated `GET /api/health` for liveness and `GET /login` for browser authentication. `GET /api/ready` runs database/data-dir readiness checks; it is **access-controlled** in this deployment and should respond with HTTP 401 to an unauthenticated request.

On an authorized machine, check the private preview endpoint using:

```bash
python3 scripts/check_native_preview.py --base-url "http://<TAILSCALE_HOST>:<PREVIEW_PORT>"
```

The script checks health, branding on the login screen, and auth enforcement. It neither prints secrets nor needs credentials. Separately verify application readiness using the authenticated endpoint or a trusted local internal check. Do not turn off authentication to make an unauthenticated readiness probe work.

User-service health is available using:

```bash
APP_UNIT="$(cat "$HOME/.config/mnerys-preview/app-unit")"
EDGE_UNIT="$(cat "$HOME/.config/mnerys-preview/edge-unit")"
systemctl --user is-active "$APP_UNIT"
systemctl --user is-active "$EDGE_UNIT"
```

The preview login page was actually rendered in a headless Chromium browser at **desktop and mobile viewports** without displaying personal conversation data. Full authenticated chat/agent workflows and Tailscale connectivity from the owner's separate client device have **not** yet been end-to-end tested.

## Deployment policy: validation and rollback

**Decision:** Use a native **deploy with validation and rollback**. A brief, controlled interruption when restarting the application is acceptable; prioritize data integrity, repeatability and reliable recovery. The private Tailscale gateway and its fixed port remain configured independently of application releases.

**Implemented native deployment workflow:**

1. Prepare a candidate release from a **clean `main` checkout** using a fixed Git revision and a separate Python 3.12 virtual environment. The running version stays active during preparation.
2. Validate the candidate with selected regression tests and JavaScript syntax checks. This avoids starting duplicate agents, schedulers or background workers against live state.
3. Check for database-sensitive source changes and **refuse migration-risk revisions** without a separately reviewed migration and recovery plan.
4. Take a **private, restricted snapshot** of persistent files and remaining SQLite databases (via the SQLite online backup API). For a PostgreSQL-backed preview, also create and verify a PostgreSQL 17 custom-format archive with the private backup helper **before** promotion. Keep previous releases.
5. Update the native `systemd` service and private source revision, then perform a **short, controlled restart**.
6. Verify the live revision, health, login, protected readiness route, and actual database/data-directory readiness via a trusted local check.
7. If the candidate fails and database schemas are unchanged, automatically restore the previous application code and service configuration, restart, and validate recovery. **Never automatically overwrite the live data** from a snapshot. If the schema unexpectedly changes, stop the app for manual recovery instead of running incompatible old code.
8. Keep the Tailscale gateway, its **private fixed high port**, and independent dependencies unchanged.

Run on the preview server from the Mnerys checkout:

```bash
# Stage/install/test the candidate without restarting the live app
python3 scripts/deploy_native_preview.py --check

# Repeat validation, snapshot data, restart and verify; rollback on failed checks
python3 scripts/deploy_native_preview.py --deploy
```

The runner reads service names from protected private host files and uses a local filesystem lock to prevent concurrent promotions. Failed tests or preparation never interrupt the running service. Versions already deployed are reported as unchanged. Private runtime credentials, addresses, ports, snapshots and host paths never belong in the repository.

**Database backend:** SQLite remains the application default, while the maintainer preview is **currently running PostgreSQL 17** for its primary database. PostgreSQL support and explicit main-database transfer are documented in [PostgreSQL 17](POSTGRESQL17.md). The native deployment runner supports PostgreSQL only with a **privately configured executable backup helper** that creates a PostgreSQL 17 custom archive and verifies it with the PostgreSQL 17 restore tooling before any restart. A private archive was also successfully restored into an isolated PostgreSQL 17 instance to validate recovery. Without the helper, promotion fails closed. Application-code rollback checks the PostgreSQL schema fingerprint and never rewrites migrated data automatically. Future database-backend changes still require an independently approved, offline cutover plan.

**Limitations and scope:** No concurrent live-version deployment is attempted; a short interruption is expected. `GET /api/ready` stays authenticated: the script checks its access guard externally and uses a **separate trusted local readiness check** for database and storage. Running two application processes against shared SQLite/background jobs is unsafe. Candidate tests validate code; they do **not** exercise a full second running instance. The rollback procedure automatically reverses **application code and service configuration**, not user data or an irreversible schema migration; those require review and explicit authorization. No automatic periodic deployment is configured: each rollout must be intentionally invoked.

#### M2 schema and feature switches (November 6, 2026)

The M2 code adds three additive schema upgrades that **fail the app
startup on PostgreSQL 17 until installed**:

- `sessions.revision`, `sessions.incarnation_id` and
  `background_tool_jobs.origin_incarnation_id` for revision/incarnation
  write fencing (`scripts/migrations/session_revision_fencing_v1.py`).
- `gateway_turns`, `gateway_turn_events` and `gateway_approvals` for
  durable turn/event/approval records
  (`scripts/migrations/gateway_turn_records_v1.py`).

Both migrations are read-only by default (`--check`) and refuse `--apply`
without a verified backup/restore and the explicit approval environment
flag. The migrations are additive and code-rollback safe: a pre-M2 build
ignores the extra columns/tables.

**Rollout executed 2026-11-06 (authorized):** a fresh restricted
PostgreSQL archive was created with the private backup helper, verified,
and restored into an isolated PostgreSQL 17 container (17 sessions, 75
messages, model endpoint rows preserved). Both migrations were then
applied in order (fencing columns with per-row backfill, durable record
tables) and the native runner deployed revision `7f4aa8b5a32a` with
liveness, trusted readiness, login and auth-enforcement checks passing.
The deployed service runs the M2 build against the migrated schema; the
deployment guard now accepts only operator-attested migration plans and
the startup schema guard remains fail-closed for the application.

The production Web turn now runs through the Gateway composition in the
private preview: `MNERYS_M21_WEB_CORE_CUTOVER=1` was added to the private
runtime environment and the service restarted (authorized 2026-11-06).
The live `/api/chat_stream` executed a real provider-backed chat turn and
an Agent-mode turn with the native `manage_notes` tool through the
composition (stream 200, `turn_mode`/`model_info` frames, deltas,
terminal `[DONE]`, policy denial rendered correctly when the inherited
external-context policy blocked a private read), and the fenced writers
persisted the turns (revision/incarnation on the test session rows; the
idempotent migration repaired one late row created by the then-unfenced
task scheduler, which now allocates fences itself). Automated browser
tests used an isolated, non-admin test user that was deleted together
with its test sessions afterwards; this is separate from Yuri's manual
logged-in validation of the legacy path on the same day. Commit
`7f4aa8b5a32a` carried the rollout; later commits carry these records.

## Cutover configuration persistence (November 6, 2026)

The Web cutover (`MNERYS_M21_WEB_CORE_CUTOVER`) is an explicitly managed
operator setting in the private runtime environment file consumed by the
systemd unit (`EnvironmentFile=`). The native deployment runner promotes
releases by rewriting only the `ODYSSEUS_SOURCE_COMMIT` line of that file,
so the cutover setting survives candidate preparation, release promotion,
restart and compatible rollback unchanged. After every promoted restart
the runner verifies that the live service process actually carries the
expected setting (it reads `/proc/<pid>/environ` through the unit's
MainPID) and fails the deployment if the effective configuration drifts;
a hardcoded default is never used, so a deliberately disabled flag is
never silently re-enabled.

**Disabling the cutover for emergency rollback:** remove (or comment) the
`MNERYS_M21_WEB_CORE_CUTOVER=1` line in the private runtime environment
file and restart the application service; the legacy Web path returns
without any database or data change. A subsequent deployment preserves
the disabled state.

New or unmigrated installations keep the default legacy behavior: the
flag is only ever set explicitly by an operator who has completed the
migration and acceptance requirements.

## Rollback and retired service

For normal startup failures the runner attempts a verified rollback to the previously healthy code, keeping the original data and backup intact. If rollback itself fails or the database schema changes, preserve the backup, stop unsafe operations and investigate before any manual state restoration. Do not restore persistent data blindly; coordinate migrations and validate compatibility.

The former Odysseus *web app* service remains installed but disabled for recovery purposes, and its original data remains in place. Its service can be reenabled only after validating dependencies, address conflicts and data consistency. This should be a deliberate recovery decision, not an automatic fallback that makes two active background workers share state.

**Never copy private runtime config, URLs or infrastructure topology into this page or a public commit.**
