# M3 acceptance — global mnerys CLI

Status: **OPERATIONALLY VERIFIED (November 6, 2026)**. The private preview
runs the authorized M3 revision and an actually installed `mnerys` CLI
completes authenticated turns against that real Gateway.

## Gate evidence

| Requirement | Evidence |
| --- | --- |
| Real packaged `mnerys` installs cleanly at user level | `tests/test_mnerys_cli_packaging.py`: uv builds wheel and sdist; a fresh venv install exposes the console script; `mnerys --version`/`--help` work even without optional deps installed. The preview acceptance used `uv tool install` on the package directory; the global console script answered version 0.1.0. |
| Remote mode runs a genuine authenticated turn against the Gateway | Loopback fixture suite (`tests/test_mnerys_cli_live_gateway.py`) exercises the installed binary over real HTTP with the production v1 router: chat stream, unicode, discovery, doctor, JSON output, invalid-token exit 4 and SIGINT exact-turn cancellation. Additionally, a least-privileged `m3-e2e` identity with a short-lived API token was created on the private preview through the application's own token mechanism, and the installed CLI completed real provider-backed turns: `2+2` -> `4` and `capital of Brazil` -> `Brasília` via the preview's NVIDIA endpoint (minimax-m3.1-flash). |
| Local mode shares the runtime, no duplicate engine | `mnerys serve` executes uvicorn `app:app` under `MNERYS_APP_DIR` (optionally `MNERYS_APP_PYTHON`); local chat uses the same Gateway contract; the live test boots serve and completes a local chat round trip. |
| Session/streaming/cancellation/auth/tool-policy contract | The CLI only consumes the server-side API v1: it never renders approval decisions (approval-required state is printed with the authorized path), never executes server data, and maps 401/403 to exit 4. Preview checks: chat with `--json` returned the JSON envelope, SIGINT produced `[cancelled]`, invalid token exited 4, a turn against another owner's session returned 404, replaying the same idempotency key returned 409, and tool-bearing agent turns ran with zero tool executions (approval boundary honored server-side). |
| Remote-only install stays lightweight | Packaging test asserts the installed wheel carries httpx but no fastapi/uvicorn/sqlalchemy/pydantic; the entry point is import-lazy. Verified against the running preview without touching the Web dependency stack. |
| Cross-platform evidence | Linux verified in CI (packaging, commands, live acceptance). Windows/macOS branches for config paths, executables and signals are present but documented as unverified. |
| Upgrade/uninstall leave server data alone | `mnerys-cli` uninstall removes the console script from the test venv; configuration and server data live outside the package. The preview identity, token and test sessions were removed after acceptance; the CLI configuration lived under an isolated `XDG_CONFIG_HOME`. |
| Persistence and turn records on real turns | Remote turns persisted both user and assistant messages in the preview database and finalized durable turn records (`status=completed` with a terminal timestamp). |
| Docs and website show M3 complete; GitHub Pages verified | `docs/cli.md`, docs README, ROADMAP; the landing page marks M3 Complete and the CLI as an active adapter; the published GitHub Pages content was fetched and asserted. |
| Full project tests and CI/security pass | Local regression suites (CLI, Gateway v1, Web cutover) pass; the published batch runs the full CI matrix on GitHub Actions, which completed 100% green and published a semantic release. |
| Three remotes synchronized | GitHub primary, GitLab/YuGit mirrors; verified hashes on each push. |

## Operational rollout details

- Previous healthy preview revision: `143e96c21c3c` (M3 implementation head).
- Deployed revision: `1050f802` (rollout fixes: v1 mounted on the real
  application, shared services exposed to the router state, durable turn
  records and terminal finalization, user/assistant persistence through the
  Web writer).
- Backup verified before the rollout (`m3-rollout-20261106T0915.pgcustom`,
  `POSTGRES17_ARCHIVE_TOC_VERIFIED True`); database and user data preserved.
- Web Core cutover remains active (`MNERYS_M21_WEB_CORE_CUTOVER=1` in the
  running process after restart).
- Rollback: the previous release directory remains deployed-side; the
  deployment runner restores the previous healthy release when validation
  fails (exercised live during this rollout).

## Outstanding

- Public PyPI publication remains a separate future decision.
- Windows/macOS installation acceptance on real runners is still unverified.