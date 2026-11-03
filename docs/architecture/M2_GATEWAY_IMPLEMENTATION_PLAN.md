# M2 execution plan — authoritative Agent Core and Gateway

**Status: planned, not implemented.** This is the technical handoff from the [M1 design exit review](M1_EXIT_REVIEW.md). The target is a **single server-owned Gateway/Agent Core workflow** shared safely by Web and future clients. The full [product roadmap](../ROADMAP.md) also defines CLI packaging (M3), TUI (M4), cross-client UX parity (M5), and installation/release reliability (M6); this plan must not mark those deliverables as shipped.

Read the [M1 source audit](AGENT_CORE_GATEWAY_M1_AUDIT.md), [domain/API contract](GATEWAY_AGENT_CONTRACT.md), [session writer inventory](SESSION_WRITER_INVENTORY.md), [revision/migration plan](SESSION_REVISION_MIGRATION.md) and [regression baseline](M1_CONTRACT_TEST_BASELINE.md) before implementation.

## Decision log

| Decision | Constraint |
| --- | --- |
| **One Python runtime** | Keep using the existing agent loop and hardened dispatcher; extract them behind a server service instead of cloning them into Web, CLI or TUI |
| **The Gateway owns authority** | Authenticate each transport and derive a trusted Principal; ownership, scopes, policies, tool approvals and effects remain server-enforced |
| **Web is the first real adapter** | Preserve current HTTP endpoints during migration; define a versioned Gateway API alongside only after explicit authorization and security regression coverage |
| **SQLite and PostgreSQL 17 are separate supported primary backends** | No assumption that production data are migrated by switching a URL; test transactions, DDL, rollback and restore on each backend |
| **Compatibility before cutover** | Preserve existing sessions, messages, preferences, tool decisions and background jobs; deploy to the private preview only after an approved rollout |
| **No false exactly-once guarantee** | Use idempotency keys and durable receipts for retried operations, with explicit reconciliation of ambiguous effects; refuse unsafe replays |

## Execution order and deliverables

### M2.1 — Finish the internal authority and turn boundary

**Goal:** one owner-scoped, transport-neutral service that controls a turn before the agent loop starts.

- Inventory the current `routes/chat_routes.py` turn admission, request authority, tool policy, model/context selection and persistence sequencing against the [existing audits](AGENT_CORE_GATEWAY_M1_AUDIT.md).
- Extract the remaining application policy and trusted request-authority construction into server-owned services. Route adapters must supply **only validated principal context and untrusted request arguments**.
- Keep the existing `src/tool_execution.py` dispatcher and sealed `src/tool_approvals.py` behavior; deny operations lacking bound authority. Preserve optional MCP, memory, model and skills integrations.
- Define concrete Python service protocols and typed errors for owner missing, forbidden, conflict, stale turn, expired cursor and unsupported capability. Keep SSE serialization in the transport layer.
- Add Web owner/bearer scope, direct/fake tool invocation, nested task inheritance and denial-path regressions.

**Exit proof:** a single real Web turn can be started, observed and stopped through the new application service with identical effective tool/security behavior. No public remote API yet.

### M2.2 — Design, migrate and verify durable session writes

**Goal:** all existing session writers participate in a lifetime/revision guard.

- Use the [full writer inventory](SESSION_WRITER_INVENTORY.md) and enumerate direct writes from Web, session tools, background jobs, import/export, cleanup, admin operations and task runners. Treat unlisted direct SQL writers as blockers.
- Design additive session `revision` and `incarnation_id` fields plus background-job `origin_incarnation_id` binding. Specify backfill for existing rows and new generation allocation on session delete/recreate.
- Verify **backup and restoration** on isolated, disposable copies of both actual primary schemas; SQLite may require a transactional table rebuild, while PostgreSQL uses separately reviewed DDL.
- Adapt append, replace/truncate, rename, archive, delete, job delivery and cache reload to **owner + incarnation + expected revision** checks in atomic transactions. Preserve upload reservations, referential integrity, message counts and idempotent delivery receipts.
- Reject stale generations rather than bypass the guard; when cached state is stale, reload after commit/failed conflict, not optimistic blind append.
- Test concurrent writers, two processes, restart, deleted/recreated sessions, pending jobs, database faults, metadata parity and cancellation during persistence.

**Stop condition:** do not activate fencing on any persistent installation while a writer bypasses the protocol, or without verified backup/restore and **separate database migration approval**.

### M2.3 — Durable turn, event and approval control plane

**Goal:** client-visible streams and approval decisions survive process replacement truthfully.

- Introduce owner-checked turn records with durable status, exact turn ID, session generation, idempotency key, effective policy version and cancellation token.
- Persist ordered, versioned events with a per-turn sequence and deterministic terminal status. Define bounded retention, pagination, explicit gap handling and what is forbidden in client-visible event payloads.
- Persist sealed, owner/session/turn/resource-bound approval records; enforce digest validation, expiry, auditing and atomic single-use consumption. Reconnection or restart must never transform historical UI data into an approval grant.
- Keep effect receipts for ambiguous tool actions, preventing a retry from silently executing an external side effect again.
- Define failure and recovery behavior for interrupted model calls, cancellations, detached jobs, nested tasks and scheduler delivery under the original authority.

**Exit proof:** restart/concurrent-worker tests preserve ordering and deny unauthorized or duplicated approvals without asserting unsupported exactly-once effects.

### M2.4 — Real transport contract and cross-adapter acceptance

**Goal:** a versioned public Gateway service with at least two **real** server-admitted transport adapters, not only fake principal projections.

- Adapt the existing Web UI and API to the shared server service without breaking legacy endpoints.
- Implement the first independent scripted remote client adapter for authenticated acceptance testing. It must not import the Web server, session database, scheduler or tool executor. This is **Gateway transport proof**, not the global `mnerys` CLI distribution (M3) or the TUI (M4).
- Define and version the public request/response schema for sessions, agents/models, turns, tool proposals, approvals, events, cancel and resume from [the contract](GATEWAY_AGENT_CONTRACT.md). Implement capability discovery that never grants execution rights.
- Run the same exact-session test cases from Web and remote client: owner authorization, policy/tool denial, idempotent create, ordered event replay, cancellation by exact turn ID, approval acceptance/rejection, disconnected retry, restart, and model/provider fallback.
- No adapter may invoke model/tool execution or DB writes directly; test explicit attempted bypasses.

**Exit proof:** two actual transports exercise the same authoritative state and policy with no client-specific grants or duplicate runtime.

### M2.5 — Controlled compatibility rollout

- Validate tests and DB migrations against separate SQLite and PostgreSQL 17 fixtures, including realistic restore drills; account for auxiliary stores independently.
- Run source/privacy checks, syntax/static checks, relevant unit and Web integration tests, and security-oriented negative tests. Document optional skipped checks rather than inferring success.
- Prepare monitored migration, staged code rollback, **separate DB recovery**, health/readiness checks and rollback criteria. Do not attempt destructive migration or preview deploy without explicit approval.
- Keep the existing private native preview accessible during development; a short planned interruption is acceptable. Do not silently replace native systemd/Uvicorn with Docker Compose.
- Update the roadmap, source architecture, public API contract, tests, deployment and recovery documentation alongside implementation changes.

## M2 acceptance matrix

| Capability | Required negative or positive evidence |
| --- | --- |
| Authenticated session operations | Both real adapters share owner-scoped session history and reject foreign or unscoped access |
| Turn admission and cancellation | One active writer/session; stale turn IDs cannot stop new turns; retries use explicit idempotency |
| Tool policy and approvals | Tool execution without bound server authority fails before effects; exact sealed approvals cannot cross users, turns, resources or restarts |
| Client-visible events | Strict monotonic sequence, deterministic terminal state, explicit retention gaps, restart-safe recovery and sanitized payloads |
| Background jobs and tasks | Original owner, session incarnation and tool authority survive scheduler delivery; stale jobs fail closed |
| Database parity | SQLite and PostgreSQL 17 migrations, concurrent writers, data preservation, backup restore and rollback demonstrated separately |
| Cross-transport compatibility | A real Web adapter and an independent scripted remote adapter use the same Gateway contract; no parallel agent or permission path |
| Security and operations | Latest commit passes full CI, negative authorization tests, deployment checks and approved rollback validation |

**M2 completion cannot be declared** until this matrix has verified implementation evidence. The independent installable CLI/TUI and full UX feature parity remain later milestone obligations.

## Documentation and operational references

- [M1 design exit and remaining gaps](M1_EXIT_REVIEW.md)
- [Proposed Gateway domain objects and routes](GATEWAY_AGENT_CONTRACT.md)
- [Session writer migration requirements](SESSION_REVISION_MIGRATION.md)
- [Native preview rollout and recovery](../deployment/NATIVE_PREVIEW.md)
- [PostgreSQL 17 transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html)
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLAlchemy versioned persistence](https://docs.sqlalchemy.org/en/20/orm/versioning.html)
- [Python protocols](https://docs.python.org/3/library/typing.html#typing.Protocol)

### First implementation slice

Start with **M2.1**: audit actual request authority construction and the agent turn lifecycle, define one server-owned admission/turn service entry point, add owner/scope and unauthorized-tool regressions, and preserve current Web behavior. Treat all public routes, database schema changes, background jobs and preview deployments as separate follow-on scopes with their own validations.
