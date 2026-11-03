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

## Deployment policy: validation and rollback

**Decision:** Use a native **deploy with validation and rollback**. A brief, controlled interruption when restarting the application is acceptable; prioritize data integrity, repeatability and reliable recovery. The private Tailscale gateway and its fixed port remain configured independently of application releases.

**Target update workflow (not yet automated):**

1. Prepare an immutable candidate release and its own Python virtual environment, without changing the live version.
2. Run tests and validate the candidate with isolated test data; check compatibility, liveness, readiness and login before promotion.
3. Keep the last known healthy release and a verified backup of persistent state. Plan reversible migrations before proceeding.
4. Switch the native `systemd` application service to the validated release with a **short, controlled restart**.
5. Recheck health, authenticated readiness and login. If any critical check fails, **roll back to the last healthy release**, restart and verify again. Restore data only under an explicitly safe, compatible recovery plan.
6. Update `/docs`, review/sanitize diffs and screenshots, and publish approved changes to all three remotes.

The imported runtime uses SQLite and runs scheduled/background work. **Do not start two live application workers against the same persistent state**, because duplicate jobs and conflicting writes may result. If a migration or data operation poses additional risk, explain it and request approval before proceeding.

**Current implementation:** the native preview already runs under `systemd` with a private gateway and working health checks. The staged promotion, automatic post-deploy validation and automatic rollback pipeline **still need to be implemented**. Until then, deployment and rollback are manual procedures; the steps above are the agreed target, not a claim of existing automation.

### Rollback and retired service

For now, recover manually by selecting the last verified release and compatible configuration, preserving state, restarting the application and revalidating health and readiness. A short service interruption is expected; never replace or roll back persistent data without a verified recovery plan.

The former Odysseus *web app* service remains installed but disabled for recovery purposes, and its original data remains in place. Its service can be reenabled only after validating dependencies, address conflicts and data consistency. This should be a deliberate recovery decision, not an automatic fallback that makes two active background workers share state.

**Never copy private runtime config, URLs or infrastructure topology into this page or a public commit.**
