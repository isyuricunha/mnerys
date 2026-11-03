# M1 — Baseline for shared Gateway contract regression tests

**Status:** first characterization slice implemented and tested on 2026-11-03; internal `TurnService` and owner/scope admission seams are present, but the complete authenticated Gateway and shared contract remain **planned**. These tests protect inherited HTTP behavior, not a new API.

## What is frozen in this slice

`tests/test_mnerys_m1_gateway_boundary_baseline.py` exercises the **existing** FastAPI chat-run routes with an isolated database double and request identity supplied as if validated by the authentication middleware. This is deliberately not a test of token parsing or login verification.

| Inherited boundary | Test evidence | Current behavior |
| --- | --- | --- |
| Session ownership across browser and bearer clients | 8 HTTP parameter cases, covering resume, stream status, stop and finish | A foreign user receives 404 before detached-run state is observed or changed |
| Bearer scope on the same routes | 4 HTTP parameter cases | A token lacking the `chat` scope receives 403 before run access |
| Same-owner browser and scoped bearer | 2 HTTP cases over all four routes | Each reaches the same session run with the supplied exact run ID |
| Replaced run / stale cancellation | 1 in-memory asynchronous race | An obsolete run ID or missing ID cannot cancel the newer active run |

**15 new tests passed.** The bounded combined regression suite (this file plus the eight inherited test files below) passed **235 tests** on the development environment.

The fake middleware sets request state, not real authentication credentials. The database query is mocked; no production database, server, external model or remote tool is involved. The real FastAPI dependency wiring, owner-check function, route handlers and in-memory run manager are exercised as described above.

## Existing regression evidence retained

- `tests/test_session_owner_attribution.py` — cookie/bearer effective owner and owner-scoped session lookups.
- `tests/test_api_token_tool_authority.py` — delegated token cannot self-approve tool actions or acquire owner execution authority.
- `tests/test_request_authority.py` — malformed/absent authority fails closed, effective tool policy and nested authority limits.
- `tests/test_tool_approvals.py` — exact action sealing, owner/session binding, expiry and single-consumption behavior.
- `tests/test_agent_runs_resume_cursor.py` — optional in-process SSE cursor, replacement and replay integrity.
- `tests/test_agent_runs_terminal_order.py` — final event timing and exact run finish identity.
- `tests/test_turn_contract_integration.py` — tool offerings do not override dispatcher policy.
- `tests/test_session_manager_persist_guard.py` — writes cannot resurrect deleted session records.

To rerun the characterization and neighboring regression baseline from the repository root:

```bash
python -m pytest -q tests/test_mnerys_m1_gateway_boundary_baseline.py \
  tests/test_session_owner_attribution.py tests/test_api_token_tool_authority.py \
  tests/test_request_authority.py tests/test_tool_approvals.py \
  tests/test_agent_runs_resume_cursor.py tests/test_agent_runs_terminal_order.py \
  tests/test_turn_contract_integration.py tests/test_session_manager_persist_guard.py
```

## Gaps before M1 acceptance

This is **not** Gateway parity across independent Web, CLI and TUI clients: they do not yet share a new `TurnService`. The inherited run manager has an **in-memory** buffer, not a restart-durable sequence/event journal. The current route replaces active runs; deterministic future one-writer admission/fencing remains to be specified and built. Approval storage is process-local. These tests do not validate SQL concurrency, real credential issuance, migrations, PostgreSQL restoration or a production deployment.

The subsequent internal TurnService, Gateway admission, read-only ownership lookup, and privilege-policy extraction were exercised with **649 passing local regression tests**, including internal Gateway principal and admission tests, run-control integrity, HTTP owner/scope regression, session ghost fallback, foreground model selection, context preparation, exact approvals, SSE replay, cancellation, nested invocation and stream error handling. The test suite still uses isolated fixtures, not real cross-client or restarted-server acceptance checks. Additional independent regression runs passed **157 session/owner tests** and **118 authentication/token tests**. A leaked optional-module stub in `tests/test_null_owner_gates.py` was corrected by registering it only through pytest's reversible `monkeypatch` mechanism; the isolated 35-test suite also passed.

**Next:** expand the read-only ownership lookup into a fully owner-scoped session repository with revision/fencing semantics; then incrementally move model/intent/context policy, request-authority construction and persistence behind a server-owned turn admission boundary. Do not expose the current internal facade as a public Gateway. Preserve the same tool dispatcher and existing Web endpoints. Do not introduce new public endpoints or alter the persistent preview until behavioral parity, schema/migration review and security gates are demonstrated.
