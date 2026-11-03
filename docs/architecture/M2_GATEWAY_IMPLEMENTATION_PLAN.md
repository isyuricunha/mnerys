# M2 execution plan — authoritative Agent Core and Gateway

**Status: M2.1 in progress, partial internal authority boundary implemented; the standalone authenticated Gateway and other M2 deliverables remain planned.** This is the technical handoff from the [M1 design exit review](M1_EXIT_REVIEW.md). The target is a **single server-owned Gateway/Agent Core workflow** shared safely by Web and future clients. The full [product roadmap](../ROADMAP.md) also defines CLI packaging (M3), TUI (M4), cross-client UX parity (M5), and installation/release reliability (M6); this plan must not mark those deliverables as shipped.

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

**Incremental implementation:** the Web stream route now calls [`GatewayTurns.build_authority`](../../src/gateway_turns.py), which delegates to the [admitted authority builder](../../src/gateway_turn_authority.py) using the server-issued admission proof and server-derived internal-tool classification. The service derives owner/session from the admission instead of accepting them as independent route arguments, preserves the existing intent and `ToolPolicy` builder, rejects forged/missing admission, and adds delegated-credential deny restrictions regardless of owner privileges. [Targeted tests](../../tests/test_mnerys_gateway_turn_authority.py) cover the behavior. Exact approval **issuance** still uses the inherited flow; decision consumption now goes through the internal Gateway facade into the unchanged sealed store. Context assembly, model selection, storage and tool execution remain unchanged. This is **not** completion of M2.1.

**Second incremental slice — exact approval continuation:** the Web route authenticates the caller and forwards the already-consumed server-side object to `GatewayTurns.build_authority` for session-bound grant preparation. The [authority builder](../../src/gateway_turn_authority.py) verifies its type and owner/session/workspace binding, checks the originating authority's same bindings, refuses internal-tool and delegated-token approvals, and reapplies the current tool policy plus delegated restrictions. A missing original authority results in an **empty** grant set; approval continuation does not reinterpret the resumed message as new permission. The dispatcher remains responsible for validating the sealed operation digest and claiming the exact action once. This is not a new approval issuer, a durable approval registry or a remote decision API.

[Regression coverage](../../tests/test_mnerys_gateway_approval_authority.py) includes consumed store decisions, disabled-tool precedence, workspace normalization, forged/mismatched bindings and the no-new-grants rule. An expanded local authorization/approval run passed **499 tests**, and a separate chat/context/streaming regression passed **290 tests**. Two unrelated tests that execute Bash against the actual container isolation runtime failed in the available test environment with `containment unavailable (filesystem, process_tree)`; isolation was **not** bypassed or relaxed, and their real-execution success remains unverified here. Hosted CI for this unpushed slice is not claimed.

**Third incremental slice — approval decision validation and consumption:** the Web adapter still enforces `require_user_approval_request` and rejects delegated HTTP credentials before invoking [`GatewayTurns.decide_tool_approval`](../../src/gateway_turns.py). The new [internal decision service](../../src/gateway_approval_decisions.py) independently rejects fabricated admission, delegated/internal callers, invalid decision strings and plan mode; it checks the pending record's owner/session before destructive consumption through the **same** [`ToolApprovalStore`](../../src/tool_approvals.py). The result contains only server-side pending/exact records and existing untrusted-context taint. On the Web side, HTTP errors retain their previous 400/403/409 classifications; legacy history-card resolution, deny SSE response and sealed continuation-context restoration remain unchanged. The inherited wire values deliberately map `approve` to **chat-session** scope and `approve_task` to **task** scope; no new permission widening was introduced.

[Regression tests](../../tests/test_mnerys_gateway_approval_decisions.py) cover valid/invalid decisions, token and internal request rejection, owner/session isolation, expiry, replay, plan-mode veto, taint propagation, single-user mode and a disappearing pending grant. The server-side approval store is still **process-local**, not durable or cross-worker; tool issuance, external effects and the actual dispatcher remain on inherited paths. This is **partial M2.1 implementation**, not a public API or M2 completion.

**Fourth incremental slice — model selection and fallback context:** both existing Chat endpoints now call [`GatewayTurns.resolve_foreground_policy` and `build_foreground_routes`](../../src/gateway_turns.py) with the issued session admission, rather than resolving owner-scoped preferences and candidate endpoints in the Web adapter. The [foreground route service](../../src/gateway_foreground_routes.py) derives owner identity from admission, preserves the inherited per-owner model allowlist/fallback policy and selected endpoint identity checks, and rejects forged, foreign-session or cross-owner policy reuse. It returns candidate tuples and **safe descriptors in the same order**; provider headers remain in candidate tuples, not user-visible route metadata. No model provider or fallback behavior was replaced.

When fallback is enabled, the [context factory](../../src/gateway_foreground_context.py) now receives the same admitted session and policy before shaping a **route-neutral copy of the original messages per candidate**. It calls the existing compactor with `persist=False`, carries per-candidate context limits, summaries and trim metrics, and rejects a different session object. The inherited Web turn still decides which completed route actually answered and applies **only that route's** prepared compaction. Strict-mode turns continue on the existing non-fallback path. Existing memory retrieval, prompt construction, runtime/provider dispatch, foreground event rendering and persistence remain in their original components.

[Foreground model regression tests](../../tests/test_mnerys_gateway_foreground_routes.py) and [context boundary tests](../../tests/test_mnerys_gateway_foreground_context.py), together with the original [fallback integration tests](../../tests/test_foreground_model_routing.py), cover owner allowlists, bearer/single-user principals, cross-session denial, route metadata/endpoint identity, different context windows, source-history preservation and no early compaction write. Local regressions passed **380 model/context/security tests** and **288 turn/streaming/session/tool tests**. The only shared warning was the existing pytest-asyncio fixture-scope configuration warning; hosted CI for this unpushed change has not run. This work does **not** establish model selection through an independent external Gateway client or complete M2.1.

**Fifth incremental slice — final prompt handoff:** the Web context builder still owns presets, attachment processing, memory/RAG, message persistence, and platform-specific preprocessing. After it returns, both Chat routes now call [`GatewayTurns.append_internal_context`](../../src/gateway_turns.py) to guard any server-classified internal-tool content and avoid introducing fresh user consent. The internal [prompt preparation service](../../src/gateway_prompt_preparation.py) requires the issued foreground policy and matching admitted owner, appends untrusted tool data to both history representations without duplicate entries, and delegates final strict-versus-route-neutral prompt selection to [`GatewayTurns.select_prompt_messages`](../../src/gateway_turns.py). For detached streams, the inherited current-request consistency check is retained; exact approval continuations keep the original transcript **without appending unrelated composer text**. Diagnostic warnings no longer log excerpts of potentially private conversation content.

This is a *bounded handoff extraction*, **not** a claim that the complete `build_chat_context` pipeline or provider invocation is transport independent. The subsequent work must separate request-specific adapters, context injection, storage and post-response processing before the full turn is owned by the Gateway. [Focused tests](../../tests/test_mnerys_gateway_prompt_preparation.py) exercise forged/foreign admission, context-owner mismatches, strict and fallback histories, multimedia messages, prompt-guard delimiter escaping, single-user mode, and approval continuation. Local regression results: **203 targeted tests** and **508 broader integration tests** passed, with only the existing pytest-asyncio fixture-scope warning. These suites overlap; their totals must not be interpreted as 711 unique tests. Hosted CI and preview deployment have not been run for this local slice.

**Sixth incremental slice — HTTP-independent shared context builder:** the inherited [`build_chat_context`](../../routes/chat_helpers.py) now acts as an HTTP adapter that resolves the trusted request principal, preserves the original message-event callback and forwards all existing options into `build_chat_context_core`. The shared implementation accepts an explicit server-side owner and optional notification callback; **it never reads an HTTP `Request` or resolves identity from headers/cookies**. Both real Web endpoints now provide their issued `AdmittedSession`, and the [context admission check](../../src/gateway_context_admission.py) validates the session ID, loaded owner when available, and request principal before any preprocessing, history mutation, memory/RAG lookup or message event. The legacy no-admission helper entry point remains for compatibility with existing internal callers.

The original preset, preprocessing, memory/RAG, attachment, compact/trim and persistence integrations continue to run in their original order and module. This is a **request-dependency seam**, not a fully transport-independent context service or a new Gateway endpoint: the context algorithm still lives in `routes/chat_helpers.py`, legacy event publication still occurs in the HTTP adapter, and unrelated services still depend on process-local database and Web components. The next extraction must introduce explicit service ports and move ownership of these integrations without duplicating them or changing persistence.

[Focused admitted-context tests](../../tests/test_mnerys_gateway_context_assembly.py) exercise real issued and forged grants, mismatched HTTP principal/session, cross-owner cached session objects, pre-side-effect rejection, named/single-user compatibility, direct no-HTTP core use, preserved owner-scoped preferences/compaction, and event/persistence suppression for internal continuations. The complete local [context and gateway regression](../../tests/test_chat_helpers.py) passed **787 tests**, with one known pytest-asyncio fixture-scope warning. No hosted CI or private preview rollout has been performed for this local slice.

**Seventh incremental slice — context dependency ports:** the common context algorithm now consumes the internal [ChatContextPorts protocol](M2_CONTEXT_SERVICE_PORTS.md), with a [legacy adapter](../../routes/chat_helpers.py) that delegates to existing handlers and stores. This exposes separate service operations for presets and attachment preprocessing, owner preferences, upload manifests, memory/RAG/Web prefaces, used-memory tracking, persisted or incognito messages and compaction/trim. **There is only one live implementation**; the new interface makes existing effects replaceable in isolated tests, not independently durable or generally available over the network. The original message/event ordering, context filters and safety checks remain unchanged.

[Port tests](../../tests/test_mnerys_gateway_context_ports.py) inject a fully in-memory adapter, explicitly reject unexpected legacy service calls and confirm persisted-message ordering, incognito isolation and no write/event on internal continuation. Together with the existing Gateway/Chat/context/permission regression, **790 local tests passed** (one known pytest-asyncio configuration warning). Other context helpers remain module-bound; the next slices should isolate model normalization and remaining storage/time/source dependencies before moving the algorithm into an independent Core package. Hosted CI and live preview verification have **not** run for this local change.

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
