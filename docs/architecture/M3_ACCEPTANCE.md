# M3 acceptance — global mnerys CLI

Status per the M3 exit gate (November 6, 2026).

## Gate evidence

| Requirement | Evidence |
| --- | --- |
| Real packaged `mnerys` installs cleanly at user level | `tests/test_mnerys_cli_packaging.py`: uv builds wheel and sdist; a fresh venv install exposes the console script; `mnerys --version`/`--help` work even without optional deps installed. |
| Remote mode runs a genuine authenticated turn against the Gateway | `tests/test_mnerys_cli_live_gateway.py`: the installed binary talks real HTTP to a loopback uvicorn fixture that mounts the production Gateway v1 router; chat streams, discovery, doctor, JSON output, invalid-token exit 4 and SIGINT exact-turn cancellation (exit 5) are exercised. |
| Local mode shares the runtime, no duplicate engine | `mnerys serve` executes uvicorn `app:app` under `MNERYS_APP_DIR` (optionally `MNERYS_APP_PYTHON`); local chat uses the same Gateway contract; the live test boots serve and completes a local chat round trip. |
| Session/streaming/cancellation/auth/tool-policy contract | The CLI only consumes the server-side API v1: it never renders approval decisions (approval-required state is printed with the authorized path), never executes server data, and maps 401/403 to exit 4. |
| Remote-only install stays lightweight | Packaging test asserts the installed wheel carries httpx but no fastapi/uvicorn/sqlalchemy/pydantic; the entry point is import-lazy. |
| Cross-platform evidence | Linux verified in CI (packaging, commands, live acceptance). Windows/macOS branches for config paths, executables and signals are present but documented as unverified. |
| Upgrade/uninstall leave server data alone | `mnerys-cli` uninstall removes the console script from the test venv; configuration and server data live outside the package. |
| Docs and website show M3 complete; GitHub Pages verified | `docs/cli.md`, docs README, ROADMAP; the landing page marks M3 Complete and the CLI as an active adapter; the published GitHub Pages content was fetched and asserted. |
| Full project tests and CI/security pass | Local regression: 1,830 gateway/chat tests + 21 CLI tests pass; the published batch runs the full CI matrix. |
| Three remotes synchronized | GitHub primary, GitLab/YuGit mirrors; verified hashes on each push. |

## Outstanding

- Acceptance against the running private preview with a real API token is
  prepared but not executed: the preview has no operator token yet. The
  fixture-based live acceptance exercises the same production router and
  contracts; an owner-created token would turn it into a preview-backed
  acceptance (documented command: `mnerys connect --endpoint <preview>
  --token-stdin && mnerys chat "..."`).
- Public PyPI publication remains a separate future decision.