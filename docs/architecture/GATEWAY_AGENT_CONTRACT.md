# Proposed Gateway / Agent Core contract (M1)

**Status: M1 design baseline completed; public v1 API and durable schema remain proposals.** The first internal runtime and admission seams are implemented, but the cross-transport contract is **not** a deployed service.
Related: [M1 exit review](M1_EXIT_REVIEW.md), [M2 execution plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md), [M1 source audit](AGENT_CORE_GATEWAY_M1_AUDIT.md), [Mnerys roadmap](../ROADMAP.md).

## Goal and boundaries

Preserve the inherited Python agent loop, tool dispatcher, request authority and session data while making execution independent of the transport. The Gateway owns authentication, user/session access, run admission, authorization, approval decisions, events and cancellations. Agent Core owns model/agent reasoning, tool proposals and execution **only when mediated through the same server-owned policy checks**.

The Web application becomes one Gateway client/adapter, not an authority source. CLI and TUI clients never import server app.py and do not access the database directly.

## Shared domain objects — proposed v1

| Type | Required semantic fields | Authority |
| --- | --- | --- |
| Principal | immutable principal_id, authentication method, granted scopes, role/privileges, trusted server context | Gateway only, derived from validated credentials |
| SessionRef | session_id, owner_id, revision, archived/active state, selected agent/model preferences | Server repository; owner-checked on every operation |
| TurnRequest | session_id, text/structured user input references, requested mode, optional model/agent, client idempotency key | Untrusted input; validate sizes and references; ignore claimed privileges |
| TurnRecord | opaque turn_id, session_id, owner_id, current state, revision, created/updated times, cancellation marker, effective policy revision | Gateway transaction; one active writer per session initially |
| TurnEvent | turn_id, monotonically increasing seq, event_type, server time, validated public payload, schema_version | Gateway event store; resume via seq; sanitized for client |
| ToolProposal | canonical tool, complete arguments, immutable operation digest, effect class, resource identity and authority scope, originating turn | Agent Core output becomes a request, not authorization |
| ApprovalRecord | opaque approval_id, owner/session/turn IDs, operation digest and sealed resource, decision scope, expiry, consumed state | Server persists securely; compare-and-swap consumption |
| ExecutionReceipt | turn_id, tool call ID, bounded operation/result metadata, security decision, effect identity, reconciliation state | Server event/effect records; minimize sensitive payloads |

Client-provided model IDs, paths, bridge descriptions, scopes and capability toggles are **hints/requests**. They never become the Principal, RequestAuthority, allowed tools or approved resource identity.

## Server interfaces (Python protocols, not fixed module paths)

- **SessionRepository:** get_owned, list_owned, create, update_revision, append_message, history, archive; must check principal/owner at the service boundary.
- **TurnService:** start_turn(principal, request), get_turn, cancel_turn(expected turn ID/revision), finish_turn, stream_events(after_seq); owns idempotent creation and session-writer fencing.
- **AgentRuntimeAdapter:** iter_events(approved turn context, model resolver, tool gateway); initially wraps the existing stream_agent_loop; no Web Request or SSE formatting.
- **ToolExecutionGateway:** propose, authorize_and_execute, record receipt; invokes existing src/tool_execution.py and request authority/resource binding. Missing/invalid trusted context means deny.
- **ApprovalRepository:** create sealed pending, decide(expected version), consume once, expire and audit. Never reconstruct a grant from client-facing approval cards.
- **EventStore:** append strictly ordered records, replay after a cursor, enforce retention and explicit gap handling.
- **ModelResolver:** resolve authorized configured endpoint and fallback set; avoid sending endpoint credentials to clients.
- **MemoryProvider:** reuse the existing provider contract; mediate owner isolation, retrieval and tool-call trust through the Core.
- **TaskRunner adapter:** schedule/execute through TurnService under sealed original owner/task authority; never grant permissions by invoking the scheduler itself.

Interfaces may live together in an initial module and be split once verified; avoid a large framework or distributed services prematurely.

## State machine and concurrency — proposal

States: accepted → queued → running → awaiting_approval → running → completed, failed or cancelled.
A terminal state must not return to running. A cancellation request is idempotent, bound to the exact turn ID and owner, and must not cancel a newer turn.

A single writer per session is the initial consistency policy. Every active turn has a revision/fencing token so late background tasks, stale UI tabs or disconnected clients cannot persist into a replaced run. Explicitly define behavior for same-session concurrency (reject or queue), preserve partial output on stop, and reconcile interrupted tool effects after process loss. Do not claim exactly-once effects unless an operation-specific idempotency key and durable receipt enforce it.

## M1 incremental implementation: trusted principal and session admission

**Implemented internally; not a new authentication provider or public Gateway API.** Existing routes now project the identity established by their trusted authentication middleware into an immutable `Principal` through `routes/gateway_principal.py`. Bearer callers must have the `chat` scope and a token owner **before any session lookup**. Browser callers retain their authenticated username. Explicit `AUTH_ENABLED=false` single-user mode remains a distinct principal type, not a privilege granted to API tokens.

`src/gateway_admission.py` applies the owner and existence policy independently of HTTP and the database. The session adapter in `routes/session_routes.py` preserves **database-first owner lookup**, optional same-process ghost-session fallback and legacy 401/403/404 behavior. It returns an internal `AdmittedSession` after validating the principal and session; foreign sessions remain indistinguishable from missing sessions. All existing routes that call `_verify_session_owner` now share this policy.

`src/gateway_turns.py` requires the admission result for detached turn start, resume, stop, finish and active-status checks from the Web route. A missing, fabricated or modified grant is rejected before reaching the inherited `TurnService` (including changes to the principal or session identity after issuance). This proof is an **in-process wiring guard**, not a cryptographic authorization token or a substitute for server-validated credentials. The lower-level `TurnService` is not a client entry point.

No database tables, authentication credentials, remote endpoints, model/tool dispatch rules or published SSE formats changed. **Not yet implemented:** durable session/turn admission transactions, one-writer fencing, a portable owner-scoped repository, fully server-authorized CLI/TUI adapters, durable events or approval persistence. The legacy chat route still prepares model context, tool policy, request authority and persistence.

Regression evidence: `tests/test_mnerys_gateway_admission.py`, `tests/test_mnerys_m1_gateway_boundary_baseline.py`, `tests/test_session_owner_attribution.py` and `tests/test_session_ghost_delete.py`, together with the agent/runtime suites listed in the [M1 baseline](M1_CONTRACT_TEST_BASELINE.md).

## M1 incremental implementation: ownership lookup and privilege limits

**Implemented internally, partial ports only.** The first session repository seam is
`src/session_repository.py`, which defines a callable `SessionOwnershipLookup`
protocol and an injected `SqlSessionOwnershipLookup` adapter. The current
`routes/session_routes.py` compatibility helper forwards to that adapter using
the existing session factory/model, keeping database rows authoritative over
optional in-process ghost sessions. Connections are closed on both success and
failure. SQLite and PostgreSQL continue through the same configured SQLAlchemy
session factory; **no data conversion, migration or schema revision occurs**.
This is **not** the full owner-scoped CRUD/revision `SessionRepository` proposed
above. The admission service accepts a repository port rather than importing
FastAPI, ORM models, or database connection state.

`src/gateway_execution_policy.py` now prepares a frozen
`ExecutionPrivilegeLimits` snapshot for an admitted session from **server
privileges only**. Delegated bearer tokens retain non-admin tool restrictions
regardless of their token owner's administrator status. Existing
`can_use_bash`, `can_use_browser`, document, image, memory, research and agent
flags keep the same tool/mode results. The Web adapter applies bearer
restrictions before request intent processing and account restrictions *after*
search/tool toggles, preventing those toggles from reopening an account denial.
Malformed privilege configuration fails closed.

**What has NOT moved yet:** full model invocation and prompt/context assembly (although owner-scoped fallback resolution and per-candidate context shaping now pass through admitted Gateway services), global settings,
per-turn web intent, active document and compare/plan mode restrictions,
exact-approval issuance, HTTP authentication and persisted-card reconciliation (still in Web/inherited components), the remaining tool-contract reconciliation, persisted messages,
approval storage, or the tool dispatcher. Their existing checks remain active
on every turn, and the authenticated client boundary is still the inherited
Web/API routes. This work does not create a new CLI/TUI authorization entry
point, durable transaction, or portable authenticated Gateway API.

Tests: `tests/test_mnerys_session_repository.py`,
`tests/test_mnerys_gateway_execution_policy.py`, the Web compatibility
and security suites in [M1 regression baseline](M1_CONTRACT_TEST_BASELINE.md).
The source-level protocol and explicit resource management patterns follow
[Python 3.11 typing.Protocol](https://docs.python.org/3.11/library/typing.html#typing.Protocol)
and [SQLAlchemy 2.0 Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html).
Their references are conceptual: the existing persistence dependencies remain
as configured in Mnerys.

## M1 incremental implementation: shared persisted-message preparation

The [writer integration inventory](SESSION_WRITER_INVENTORY.md#incremental-live-compatibility-extraction-message-preparation) documents the implemented `src/session_message_preparation.py` seam. It is reused by the inherited SessionManager single-message path, transcript replacement and background-job delivery, and supplies the same immutable `PreparedSessionMessage` structure expected by the **disabled** fenced writer. Transcript replacement now assigns persisted message IDs only after a confirmed commit. The [legacy append and agent-session-tool compatibility fixes](SESSION_WRITER_INVENTORY.md#incremental-live-compatibility-fixes-append-acknowledgement-and-agent-tools) also roll back failed cached appends and avoid duplicate unscoped rename writes, while retaining the inherited database transaction and authorization model. Upload reservation, request authority, session ownership and live job delivery remain in their inherited paths. The [isolated background-delivery and cache prototype](SESSION_WRITER_INVENTORY.md#experimental-fenced-delivery-and-cache-reconciliation) validates future transaction and reconciliation behavior, **not** durable Gateway persistence.

## M1 isolated prototype: revision-checked session metadata

The [session revision and concurrency migration plan](SESSION_REVISION_MIGRATION.md) documents the **disabled-by-default** SQLAlchemy compare-and-swap prototypes: legacy revision-only metadata tests on SQLite plus an incarnation-fenced session metadata/message writer tested on isolated SQLite and PostgreSQL 17. A synthetic PostgreSQL 17 schema/backfill preview is also tested. The [writer inventory](SESSION_WRITER_INVENTORY.md) identifies the legacy routes, tools and jobs that still bypass the new writer. The current Session model lacks revision/incarnation columns; no production schema, database or deployment was modified. Full owner-scoped session CRUD, durable turn fencing and all-writer migration remain planned.

## M1 incremental implementation: internal TurnService seam

**Implemented internally, NOT the proposed public Gateway/TurnService v1 contract.** The first extraction in `src/turn_service.py` moves two existing operations out of `routes/chat_routes.py` without duplicating the Python agent engine:

- `TurnService.stream_agent(bridge, *args, **kwargs)` delegates to the inherited `stream_agent_loop`, retaining `TurnContract` and execution-bridge context for the whole stream. It explicitly closes the underlying async generator before releasing these bindings when callers disconnect, close, cancel or raise.
- `start_detached`, `resume_active`, `stop`, `finish` and `is_active` delegate to the existing `src/agent_runs.py` registry. A `TurnStream` contains the exact run ID and an async iterator of **legacy SSE-formatted frames**, separate from HTTP response construction. It is **not** the proposed typed `TurnEvent` stream. Run replacement, cancellation identity, persistence timing, replay window and frame bytes remain inherited behavior.
- The existing Web routes project validated credentials and check session ownership via the internal Gateway admission policy **before** calling this service. They still construct request authority, models, policy, approvals and persistence; downstream tool execution still uses `src/tool_execution.py`. The legacy `X-Odysseus-Run-Id` response header is retained for client compatibility.

**Security limitation:** this internal seam is not a remotely callable entry point or a standalone authorization boundary. It must **never** be exposed to CLI/TUI or a new route without server-owned principal admission and session/operation checks. Its run-state registry is process-local, and no event/approval database or migration was introduced.

Coverage: `tests/test_mnerys_turn_service.py`, `tests/test_mnerys_m1_gateway_boundary_baseline.py`, and the inherited `test_turn_contract_integration.py`, `test_client_tool_routing.py` and detached-run regressions. Next steps are typed server-owned admission, owner-scoped repository ports, policy/context orchestration and real cross-transport contract tests.

## M1 incremental implementation: process-local SSE replay cursor

The inherited detached-run manager now has a **tested opt-in primitive**, not a public Gateway API:

- `src/agent_runs.py:subscribe(session_id, expected_run, *, after_cursor=None, with_event_ids=False)` retains byte-for-byte legacy SSE frames by default.
- With `with_event_ids=True`, each published SSE frame carries an `id: <run_id>:<zero-based-seq>` header; `after_cursor` resumes strictly after that exact event.
- The run is bound to its original session. A cursor for a different/replaced run, malformed cursor or cursor beyond the current buffer is rejected rather than silently resuming the wrong stream. A previously bound response may continue consuming its original replaced run.
- These IDs are **in-memory and transient**. They do not survive restart, run eviction or changes to the detached-run implementation. This is a stepping stone toward the proposed durable event contract, **not** the event schema, event store, `/api/v1` transport, or an authorization mechanism.
- Future authenticated adapters must check session ownership **before** resolving/using a run and map missing/mismatched/expired cursors into explicit client errors or gap-state responses; never reflect raw exceptions or assume possession of a cursor authorizes access.

Regression coverage: `tests/test_agent_runs_resume_cursor.py` and `tests/test_agent_runs_terminal_order.py`. The proposed versioned event envelope, durable cursor semantics and multi-client Gateway acceptance tests below remain planned.

## Event schema — proposal

Required envelope: schema_version, turn_id, seq, type, created_at, payload.
Initial event types: turn.started, message.delta, reasoning.delta (only if policy allows), tool.proposed, tool.started, tool.result, approval.requested, approval.resolved, message.final, turn.completed, turn.failed, turn.cancelled, stream.gap.

Events must be owner-filtered and non-secret-bearing, with stable IDs and deterministic terminal ordering. On resume, the client supplies the last accepted seq; the server replays events with seq strictly greater than that cursor, then streams live events. If retention removed the cursor, respond with an explicit gap and a canonical current state; **never silently present partial history as a full replay**.

Do not expose model reasoning that the model/provider policy prohibits. Redact tool arguments/results and references based on the user's permissions rather than blindly forwarding raw internal events.

## HTTP transport sketch — NOT shipped endpoints

These are candidate routes; retain existing Web chat routes during migration.

- POST /api/v1/turns — authenticate, validate SessionRef, create turn (idempotency key).
- GET /api/v1/turns/{turn_id} — owner-scoped state snapshot.
- GET /api/v1/turns/{turn_id}/events?after_seq=N — owner-scoped SSE replay/live stream.
- POST /api/v1/turns/{turn_id}/cancel — owner-scoped cancellation, with exact turn ID.
- POST /api/v1/approvals/{approval_id}/decision — owner-checked, exact sealed approval, atomic single consumption.
- GET /api/v1/sessions and GET /api/v1/sessions/{session_id} — owner-scoped session data.
- GET /api/v1/models, GET /api/v1/agents and GET /api/v1/tools — filtered capability discovery, **not** execution permission.

CLI/TUI may use the same HTTP+SSE transport. Local CLI mode can use an in-process Gateway service facade but **must still establish a trusted local Principal and call the same authorization boundary**. Do not expose raw Python tool entry points directly to the CLI.

## Identity, authentication and approvals

1. The Gateway validates the existing login/session or scoped API credentials. Remote mode requires an authenticated private or otherwise protected endpoint; networking alone is insufficient.
2. Session ownership and role/tool privileges are checked before creating a turn, reading history, replaying events, cancelling or deciding on approval.
3. Only a server-side request admission operation may construct and bind trusted RequestAuthority and exact resource identity. Client-supplied authority blobs are never accepted.
4. Effective allowed operations are the intersection of principal scopes, server policy, session/agent settings, requested turn contract, resource confinement and inherited parent authority.
5. Execute all tools through the existing policy/approval/dispatcher path; no transport-specific administrative bypass.
6. Approval decisions apply only to their sealed operation/scope with TTL, one-time atomic consumption and audit; a reconnect or process restart must not broaden the authorization.
7. Scheduled and nested agent turns inherit/cap the original authorization. Untrusted web pages, skills, memory or tool output cannot grant permissions.
8. Persist necessary security artifacts without saving secrets or unredacted tool data into ordinary client-visible events.

## Persistence and rollout

Use existing session/message tables and primary DB backend (SQLite default; PostgreSQL 17 optional). Do not replace the session history model or discard auxiliary stores. Introduce new turn/event/approval tables only through reviewed versioned migrations, with data/backup recovery plan for both backends.

- Gate the new service behind explicit compatibility tests and a reversible rollout; keep existing Web endpoints as adapters until parity is demonstrated.
- Distinguish old data/history from durable active-turn state. Do not backfill fictional historical tool/approval events.
- Use bounded retention for event payloads and preserve minimal audit/recovery receipts; take privacy and storage costs into account.
- Support a single process first. Multi-process coordination (locks, wakeups, leases) is **not** implied merely because PostgreSQL is used.
- Run isolated database tests, authenticated HTTP/Web regression tests and migration/restore exercises before enabling on the persistent preview.

## M2 operational acceptance matrix — future tests

| Scenario | Required outcome |
| --- | --- |
| Web and CLI same authorized session | identical messages, model resolution and tool-policy decisions |
| Other user requests session/events/cancel | denied, no private data or stream content |
| Tool invocation without RequestAuthority | denied before any side effect |
| Tool disabled by owner/server policy | denied even if offered by model or tool schema |
| Modified approval input/resource/owner | rejected, approval remains unusable for alternate operation |
| Approval replay or double-consumption | exactly one successful sealed consumption |
| Cancel an obsolete turn ID | cannot affect a newer run |
| Disconnect/reconnect with cursor | ordered replay without duplicated visible output |
| Gateway restart during run | durable state reported truthfully; interrupted effects reconciled safely |
| Concurrent sends in one session | deterministic admission, no interleaved history |
| Scheduled/nested turn | cannot exceed originating owner/task authority |
| SQLite and PostgreSQL install/restore | successful regression parity, original data preserved |
| Remote client minimal install | no app startup, scheduler or server DB required |

**Status:** this proposed contract is a **completed M1 design deliverable**, not an implemented public Gateway. None of the new routes or durable semantics should be described as operational until code and tests meet the [M2 acceptance plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md).
