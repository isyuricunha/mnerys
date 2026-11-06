# M2.5 acceptance matrix status

Every row below is the acceptance evidence for
[M2_GATEWAY_IMPLEMENTATION_PLAN.md](M2_GATEWAY_IMPLEMENTATION_PLAN.md).
"Implemented" means tested with disposable SQLite and/or the disposable
PostgreSQL 17 harness, plus the published CI revision. Production schema
migration and preview deployment remain separately authorized steps; the
tables are installed only by the explicit, approval-gated migrations.

| Capability | Evidence |
| --- | --- |
| Authenticated session operations | Web adapter (`/api/chat_stream` admission, `routes/gateway_principal.py`) and remote adapter (`/api/v1/sessions`, turn endpoints) both enforce the owner scope server-side: `tests/test_mnerys_gateway_api.py::test_turns_require_an_authenticated_owner`, `test_turns_are_owner_scoped_and_bypass_is_refused`, `test_durable_records_back_snapshots_replay_and_idempotency` (foreign owner 404), and the pre-existing browser/bearer admission suites. |
| Turn admission and cancellation | One active writer per session (detached run manager cancels a previous in-flight run before starting a new one, `src/agent_runs.py`); stale turn ids cannot stop new turns (`/cancel` of an unknown/old id returns 404); retries use explicit idempotency keys with replay conflict 409 (`test_mnerys_gateway_api.py`). Exact-turn cancellation tested for both transports. |
| Tool policy and approvals | Tool execution without bound server authority fails before effects (pre-existing dispatcher/policy suites, e.g. `tests/test_mnerys_gateway_approval_authority.py`); sealed approvals cannot cross users, turns, resources or restarts: one-use sealed records bound to owner/session/incarnation/turn/tool/digest/resource/authority with replay refusal (`tests/test_mnerys_turn_records.py`, `tests/test_mnerys_turn_records_integration.py`, `test_remote_client_cannot_decide_approvals_by_design`). |
| Client-visible events | Strict monotonic per-turn sequence, deterministic terminal states, explicit retention gaps, restart-safe replay and sanitized payloads: `src/gateway_turn_records.py` plus `tests/test_mnerys_turn_records.py` (immutable sequences, visible gaps, exactly-once finalize) and `tests/test_mnerys_turn_records_integration.py::test_finalized_turn_is_readable_by_a_fresh_interpreter`. |
| Background jobs and tasks | Original owner, session incarnation and tool authority survive scheduler delivery: `src/background_tool_jobs.py` binds `origin_incarnation_id`, discards recreated-session deliveries and guards counter updates (`tests/test_background_tool_jobs.py::test_recreated_session_discards_the_stale_job`, `test_fenced_delivery_commits_message_and_bumps_revision`). |
| Database parity | SQLite and PostgreSQL 17 migrations, concurrent writers, data preservation, backup restore and rollback demonstrated separately: `tests/test_mnerys_session_fencing_migration.py`, `tests/test_mnerys_session_manager_fencing.py`, `tests/test_mnerys_turn_records.py`, `tests/test_mnerys_gateway_text_sqlite_integration.py` and the disposable PG17 drill (`scripts/drill_pg17_backup_restore.sh`, local only) which backs up a seeded fenced schema, drops the database, restores it and validates columns/backfill. The additive migrations are rollback-safe by design (code rollback to a pre-migration build is safe; the startup schema guard fails closed on PostgreSQL until the migration runs). |
| Cross-transport compatibility | The real Web adapter (production `chat_stream` through `compose_web_turn`) and the independent scripted remote adapter (`/api/v1`, `tests/remote_gateway/client.py`) share the same Gateway facade, detached run manager, sealed store and durable records; no parallel agent or permission path exists (`tests/test_mnerys_gateway_web_core_cutover.py`, `tests/test_mnerys_gateway_api.py::test_web_and_remote_share_one_snapshot_state`). |
| Security and operations | The published revision runs the full GitHub Actions CI (all pytest shards, CodeQL, secret scan, workflow security, dependency review, Trivy, container scan, PG17 compatibility). Negative authorization tests cover unauthenticated calls, wrong-scope tokens, foreign owners, delegated-token approval refusal, approval replay, stale turns, idempotency conflicts and loop-bypass source checks. Deployment and rollback documentation is updated in `docs/deployment/` and the migration scripts are approval-gated. |

## Remaining before M2 COMPLETE

- **Rollout status (2026-11-06):** the authorized preview rollout is
  complete. Fresh backup verified and restore-tested in an isolated
  PostgreSQL 17 container, both migrations applied (fencing columns
  backfilled, durable tables created), native deploy of `7f4aa8b5a32a`
  with liveness/readiness/login/auth checks passed, real-provider
  acceptance turn passed on the deployed build, and the login page
  verified in real Chromium at desktop and mobile viewports with no
  console errors. The Web chat cutover switch stays OFF until the owner
  validates the logged-in chat parity; nothing in the documented M2
  acceptance requires the switch to be on.
- **Owner logged-in validation (2026-11-06):** the owner confirmed the
  preview chat works in the browser with real authorized models; this was
  the last human acceptance step. M2 implementation, validation and the
  authorized preview rollout are complete.
- The photo-editor browser release-gate suite still requires the full
  local development boot and remains outside this environment and the CI
  jobs; the gateway-notes browser suites (5) and browser-to-HTTP
  integration suite (3) pass in installed Chromium.