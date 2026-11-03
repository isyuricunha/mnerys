# Mnerys — private native development preview

This page documents the verified native preview arrangement, not a Docker-based Mnerys app deployment.

## Runtime and operating status

The private Mnerys preview is running directly from a versioned Git source snapshot with its own Python 3.12 virtual environment and Uvicorn process. Two **user-level systemd services** are enabled for restart and automatic startup:

- `mnerys-preview-app.service` — application, listening only on loopback.
- `mnerys-preview-edge.service` — Caddy gateway, bound **only to the host's Tailscale address**.

The edge gateway has **one fixed, randomly selected high TCP port**, configured privately. The public repository deliberately contains **no actual address, hostname, internal port, token or server path**. Consult your private host configuration to retrieve access details. The browser must be connected to the authorized tailnet.

Authentication is enabled and localhost auth bypass is disabled. The legacy Odysseus user-level application service was disabled only after the Mnerys native preview passed basic health, login and browser checks. Original Odysseus data files remain preserved separately.

The Mnerys application is **not containerized**. Existing optional dependencies (e.g. search/index services) may still run under their previously configured deployment method; do not disable them simply because the old Odysseus web service was retired.

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
systemctl --user is-active mnerys-preview-app.service
systemctl --user is-active mnerys-preview-edge.service
```

The preview login page was actually rendered in a headless Chromium browser at **desktop and mobile viewports** without displaying personal conversation data. Full authenticated chat/agent workflows and Tailscale connectivity from the owner's separate client device have **not** yet been end-to-end tested.

## Deployment changes and zero-downtime limitation

The initial instance is reproducible from a checked-out source revision, private deployment settings and a Python virtual environment. **An automated blue-green promotion pipeline is not yet implemented.**

This imported app runs scheduled/background work and uses SQLite locally. Running two live copies against the same state may duplicate background effects or race on state. Conversely, staging on a copy of the data does not automatically preserve writes made to the live instance during promotion. Thus **true zero downtime with data consistency is not yet verified or promised**.

Until application-level background-worker isolation and state coordination are implemented:

1. Build and test a candidate from an immutable Git revision with a **separate** virtual environment and isolated test data.
2. Verify health, readiness, login, compatibility and a rollback plan before any traffic switch.
3. Preserve the last known healthy release and a safe backup of persistent state.
4. If safe cutover requires stopping the sole writer or migration, **explain downtime/data risks and obtain specific authorization before proceeding**.
5. Never activate two background-worker processes on the same production state merely to create a nominal blue-green deployment.
6. Update `/docs`, review/sanitize diffs and screenshots, and publish approved changes to all three remotes.

### Rollback and retired service

In a failure, keep the data and last known release intact, restore the previously verified code and configuration, and validate liveness/readiness before reopening the preview. With this architecture rollback may require a short controlled outage; do not claim otherwise.

The former Odysseus *web app* service remains installed but disabled for recovery purposes, and its original data remains in place. Its service can be reenabled only after validating dependencies, address conflicts and data consistency. This should be a deliberate recovery decision, not an automatic fallback that makes two active background workers share state.

**Never copy private runtime config, URLs or infrastructure topology into this page or a public commit.**
