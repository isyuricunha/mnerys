# Proposed Gateway / Agent Core contract (M1)

**Status: M1 design baseline completed and the v1 Gateway API is
implemented under M2.4.** The routes, the durable records, the sealed
approvals, the detached run manager and the independent scripted remote
client are implemented behind the explicit migrations and the standard
authenticated app routers; production schema migration and preview
deployment remain separately authorized. The proposals below that are
marked "planned" still describe later milestones.

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

**What has NOT moved yet:** full model invocation and the inherited preset/attachment/memory/RAG and persistence integrations (now behind the [partial context service-port seam](M2_CONTEXT_SERVICE_PORTS.md), not independent services). The shared context algorithm now takes an explicit, server-validated owner instead of the HTTP request, but is still hosted in the legacy helper module and depends on its services (while fallback, per-candidate shaping and final prompt handoff use admitted Gateway services). Other remaining work includes global settings,
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

## M2.1 incremental implementation: admitted stream delivery

**Implemented internally; not a public Gateway stream protocol.** The
[Gateway stream delivery boundary](../../src/gateway_stream_delivery.py)
requires the same server-issued session admission for both direct
response-bound and detached streams. The
[Gateway facade](../../src/gateway_turns.py) selects the lifecycle before the
[Web adapter](../../routes/chat_routes.py) constructs its existing SSE
response: Compare and explicitly unattended native requests stream directly;
interactive chat/agent requests use the inherited detached-run manager and
exact run ID for resume/cancel. Client runtime hints never supply authority.

The service has no FastAPI or route dependency and creates no second run
registry. The direct branch remains cancel-on-disconnect, while detached
delivery retains process-local replay. The existing wire header and event
frame formats remain unchanged; durable turn state, event storage, restart
recovery, authenticated remote transport and cross-client acceptance remain
planned. See the [M2 execution plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[stream-delivery regressions](../../tests/test_mnerys_gateway_stream_delivery.py).

## M2.1 incremental implementation: admitted post-response work

**Implemented internally, not a persistent task queue.** Both streaming
completions and the synchronous Chat endpoint now route post-response work
through [GatewayTurns](../../src/gateway_turns.py) and the
[admitted post-response service](../../src/gateway_post_response.py).
The service checks the issued admission, loaded session identity and owner,
and assembled context owner before passing only the server-admitted owner and
session ID to the inherited task runner. Tool-policy denial, exact approval
continuation and unattended requests retain their original limits on
auxiliary model-driven extraction.

The [legacy helper](../../routes/chat_helpers.py) still handles token
accounting, webhooks, auto-naming and sequential memory/skill extraction.
Denying auxiliary extraction does not skip other permitted completion effects.
This extraction adds no duplicate agent runtime, independent scheduler,
durable job or event record, cross-client control, or preview rollout.
See [M2.1 verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[negative authorization tests](../../tests/test_mnerys_gateway_post_response.py).

## M2.1 incremental implementation: final admitted tool policy

**Implemented internally, not a new tool engine.** The
[Gateway execution policy](../../src/gateway_execution_policy.py)
finalizes deny-only tool restrictions for an admitted, owner-matched turn,
and [GatewayTurns](../../src/gateway_turns.py) exposes this operation to the
legacy Web adapter. The adapter still resolves request intent and the
server-loaded account/global settings. The service preserves the existing
account and delegated-credential denials, global disabled tools,
auto-escalation clamp, Compare and Plan restrictions, guide-only handling,
and mode demotion when Agent is unavailable. Omitted legacy mode fields
remain supported; Compare defaults to conservative non-Agent restrictions.

This finalization performs no model invocation, tool execution, schema
selection or persistence itself. The inherited runtime and hardened
dispatcher remain the only execution engines. Test evidence and limits are
in the [M2.1 execution plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[negative policy tests](../../tests/test_mnerys_gateway_final_tool_policy.py).
A remotely authenticated Gateway, durable approvals/events and migration
of the remaining Web authority paths are still planned.

## M2.1 incremental implementation: admitted contract selection

**Implemented internally, not an independent runtime or tool engine.**
[GatewayTurns](../../src/gateway_turns.py) now selects both the compact
pre-context runtime and the later tool-contract/native-workspace eligibility
through [admission-bound rules](../../src/gateway_turn_contract_selection.py).
Both decisions require the original issued session admission and a matching
server-derived context owner. The early decision intentionally does not apply
the native-workspace exception, preserving the compact context preparation
used by the inherited agent loop. A native client marker alone is never
permission for workspace tools: late native eligibility requires compatible
terminal/workspace facts and still passes through the original tool policy and
immutable contract's deny filtering.

The actual [contract resolver](../../src/turn_contract.py), model invocation,
tool dispatcher and persistence remain shared inherited components, not
duplicate client-specific engines. See [M2.1 scope and verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
and the [negative tests](../../tests/test_mnerys_gateway_turn_contract_selection.py).
The authenticated public Gateway and durable turn/event authority are planned.

## M2.1 incremental implementation: routed contract handoff

**Implemented internally; still partial M2.1.** The
[Gateway facade](../../src/gateway_turns.py) now checks the admitted owner,
session and original request authority before calling the existing immutable
[tool-contract resolver](../../src/turn_contract.py) through
[one application service](../../src/gateway_routed_contract.py). The
returned authority is restricted by the same tool policy. A schema offered
to the model is not an execution grant, and no alternate resolver or
tool dispatcher was introduced.

The inherited Web adapter still prepares tool schemas and selected intent,
applies compact preview and experiment routing, reconciles the final
disabled-tool list and invokes the model. See the
[M2.1 incremental verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[admission/denial tests](../../tests/test_mnerys_gateway_routed_contract.py).
Durable records and independently authenticated Gateway clients remain planned.

## M2.1 incremental implementation: admitted compact scope

**Implemented internally, still partial M2.1.** The
[Gateway facade](../../src/gateway_turns.py) now invokes an
[admitted scoping service](../../src/gateway_contract_scoping.py) after the
original immutable routed contract is resolved. The service verifies the
issued admission and bound authority, then delegates to the existing compact
preview/experiment scopers and performs final policy/disabled-tool
reconciliation. Experiment selection is derived from the **admitted owner**
and the server-loaded model, never from a client-selected identity; its
request header alone is not a privilege grant. The result retains the
original request authority and cannot manufacture new operation grants.

Schema collection, request-specific routing, model calls and tool dispatch
still use their inherited implementations. There is no new transport, model
engine, process scheduler or persistent event store. See the
[M2.1 verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[negative and parity tests](../../tests/test_mnerys_gateway_contract_scoping.py).

## M2.1 incremental implementation: owner-admitted tool inventory

**Implemented internally; M2.1 remains partial.** The
[Gateway facade](../../src/gateway_turns.py) now prepares the existing
native/MCP schema inventory through the
[admitted service](../../src/gateway_tool_inventory.py), which checks
the issued session and owner before reading any tools. It retains the
inherited MCP disabled configuration, owner/delegated-denied tools,
SFT Web filesystem protection, Plan and guide-only behavior, and excludes
the environment-only `host_shell` bridge. A model schema never grants tool
execution authority; the original server-bound request authority and
dispatcher remain necessary for every operation.

The Web adapter still selects request-specific tool intent, contextual
follow-up tools and fixture capability adjustments. This extraction does
not introduce a new MCP manager, remote transport, persistent task queue,
or database migration. The [M2.1 verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
and [negative/parity tests](../../tests/test_mnerys_gateway_tool_inventory.py)
document its limits and compatibility.

## M2.1 incremental implementation: exact intent and follow-up selection

**Implemented as an internal Gateway handoff, not a new tool engine.**
[GatewayTurns](../../src/gateway_turns.py) delegates exact selected-tool and
required-name preparation to
[an admitted selection service](../../src/gateway_tool_intent.py). It
verifies session/owner binding, retains the inherited
[request classifier](../../src/turn_contract.py) and editor-preservation
rules, and uses a lazy, server-side typed-history lookup to keep explicit
browser/Web follow-ups narrowly selected. It also preserves the fixture-only
personal capability exception and open-email UI requirement without
overriding blocked tools. A client message or prior execution record can
select a candidate schema but **cannot create an executable grant**.

The Web adapter still resolves initial capability intent, active-document
and browser state, and the actual agent loop/model invocation; the
immutable resolver and dispatcher remain the only execution path.
See the [M2.1 verification plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
and [negative selection tests](../../tests/test_mnerys_gateway_tool_intent.py).
Durable turn records, the public Gateway and independently authenticated
transport clients are still planned.

## M2.1 incremental implementation: admitted initial capabilities

**Implemented internally; the full turn boundary remains partial.**
[GatewayTurns](../../src/gateway_turns.py) now calls an
[owner-admitted classifier service](../../src/gateway_turn_capabilities.py)
for the earliest candidate-family decision. It reuses the original
[request and browser-correction classifiers](../../src/turn_contract.py)
over server-loaded session history, retains the distinction between
required active families and optionally offered visible-document tools,
and inherits browser state only when no unrelated explicit family is
present. A browser correction or image-edit signal is *context and
selection metadata*, never a new operation grant.

The Web adapter still checks document and workspace ownership, classifies
its earlier transport-specific browser facts and builds attachment context;
the original policy, tool contracts and runtime execute the turn. No
standalone HTTP Gateway or durable turn/event store is provided by this
slice. See [M2.1 evidence](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
and [negative/parity tests](../../tests/test_mnerys_gateway_turn_capabilities.py).

## M2.1 incremental implementation: admitted Web intent reconciliation

**Implemented internally; not a public Gateway or independent agent runtime.**
[GatewayTurns](../../src/gateway_turns.py) now delegates post-capability
public-Web intent and Web/browser denylist composition to
[one admitted service](../../src/gateway_web_intent.py). It verifies the
issued session and owner, obtains typed contextual Web evidence through
validated lazy server ports, and preserves the original opt-in/explicit-denial
precedence, previous-chat distinction, compact-model Web behavior,
exact-public-URL exception and workspace-output restrictions. These are
selection and initial denial decisions; they **do not** create executable
grants or override the later global/account/tool policy.

The Web adapter retains initial lexical intent, route-specific browser and
workspace state, context formatting and Chat-to-Agent escalation. Model
calls, hardened tool dispatch and persistence remain inherited. See the
[M2.1 verification](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[Web reconciliation parity tests](../../tests/test_mnerys_gateway_web_intent.py).

## M2.1 incremental implementation: pure pre-admission Chat routing

**Implemented as an internal, unprivileged preview of Chat-to-Agent mode, not
as an authorization decision.** The
[Gateway facade](../../src/gateway_turns.py) now delegates the early
session-independent promotion to the
[pure routing service](../../src/gateway_initial_agent_routing.py), which
reuses the [original intent classifier](../../src/action_intents.py). It
preserves the typed-tool, search toggle, public-Web and private-browser
precedence and the old workspace shell-toggle hint without issuing a grant
or accessing a session. This stage necessarily occurs **before session
resolution and admission**; no user or workspace authorization is inferred
from it. The subsequent admitted Gateway policies and unchanged tool
dispatcher remain required for all operations.

Post-session browser follow-ups, workspace path restoration and visible
document autoescalation are still Web-managed. No external Gateway client,
new agent engine, durable event store or database change was added. See the
[M2.1 incremental evidence](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[preflight safety and parity tests](../../tests/test_mnerys_gateway_initial_agent_routing.py).

## M2.1 incremental implementation: admitted session-dependent routing

**Implemented as an internal turn-bound mode decision, not an execution
permission.** After session ownership and model reconciliation, the
[Gateway facade](../../src/gateway_turns.py) now invokes an
[admitted session routing service](../../src/gateway_post_session_routing.py)
for persisted compact-mode ownership, successful private-browser state,
referential Web/browser continuations and confined workspace-path promotion.
The Web compatibility adapter supplies lazy, server-side history and
workspace checks, and still owns the actual request-scoped path confinement.
A second admitted call after owner/session-checked visible-document lookup
preserves the original document-edit Chat-to-Agent promotion. These results
carry no tool grants and are subject to the original policy, turn authority
and dispatcher.

The Web adapter still resolves the active model, browser/workspace state and
document identity and performs legacy context/model/streaming work. A complete
Gateway transport, independent Agent Core, durable storage and cross-client
acceptance are not implemented by this slice. See the
[M2.1 verification record](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[routing parity/negative tests](../../tests/test_mnerys_gateway_post_session_routing.py).

## M2.1 incremental implementation: pure initial Web/browser intent

**Implemented internally as a pre-admission lexical classifier, not a tool
grant or authenticated Gateway.** [GatewayTurns](../../src/gateway_turns.py)
now calls the [single pure Web-intent service](../../src/gateway_initial_web_intent.py)
for explicit public URLs, search/discovery, personal-store precedence,
interactive or private browsing and workspace-local HTML rendering requests.
The Web compatibility adapter reexports inherited helpers, resolves the
search toggle and validated workspace, and computes local-rendering
necessity lazily through a server-owned callback. An explicit URL is never
an unrestricted browsing grant; all tool offers and execution remain
subject to owner-admitted policy and the existing dispatcher.

This slice does not change session admission, persistence, model execution,
SSE or the existing post-session routing. See the
[M2.1 verification record](M2_GATEWAY_IMPLEMENTATION_PLAN.md) and
[lexical parity/denial tests](../../tests/test_mnerys_gateway_initial_web_intent.py).
The independent Gateway/API, agent runtime and cross-client acceptance
remain planned.

## M2.1 incremental implementation: admitted runtime toggle state

**Implemented internally as a policy-derived runtime-context projection.**
The [Gateway facade](../../src/gateway_turns.py) now invokes an
[owner-admitted runtime-state service](../../src/gateway_turn_runtime_state.py)
after final account/delegated/global tool policy but before authority
construction. It always replaces a client-supplied `web_ui_state` with the
server's effective Web, shell and document-editor availability, and preserves
the existing RAG, research and incognito request-toggle interpretation.
This prevents untrusted runtime metadata from presenting denied tools as
available; it **does not** authorize tools or alter the existing execution
policy and dispatcher.

Web still parses UI toggles and supplies its validated runtime metadata.
The Agent Core and `ui_control` continue to consume the same context
shape. See [M2.1 implementation evidence](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
and [runtime-state parity/denial tests](../../tests/test_mnerys_gateway_turn_runtime_state.py).
Persistent turn-state reconciliation, independent authenticated transports
and cross-client execution acceptance remain planned.

## M2.1 incremental implementation: admitted Agent Loop execution

**Implemented as a guarded in-process run entry point, not an independent
transport.** [GatewayTurns.stream_admitted_agent](../../src/gateway_turns.py)
now calls the [admitted execution service](../../src/gateway_agent_execution.py)
to validate session/owner, selected model, bound RequestAuthority and final
ToolPolicy before invoking the [unchanged TurnService](../../src/turn_service.py).
Provider headers, session identity and delegated status are server-derived;
runtime overrides cannot replace them. Stream completion, early close, errors
and cancellation restore bridge/contract bindings. The model-loop behavior
is tested with a synthetic provider from Python without Web or HTTP.

**Still partial:** Web constructs policy and context before this call, and
streaming events are still inherited SSE frames. This is not the proposed
TurnRequest/TurnEvent protocol, a durable event store or a public remote API.
See the [M2.1 exit gate](M2_1_EXIT_GATE.md) and
[execution acceptance tests](../../tests/test_mnerys_gateway_agent_execution.py).

## M2.1 incremental implementation: direct text-turn composition

**Implemented as a limited, in-process application path only.** The
[turn composer](../../src/gateway_turn_composer.py) now orchestrates
owner-admitted context assembly via [injected I/O ports](M2_CONTEXT_SERVICE_PORTS.md),
server policy and restricted request authority, then the existing
Agent Loop in one call. It accepts only an untrusted text message plus
server-owned dependencies; the initial direct mode denies all native/MCP tools
and performs no conversation persistence. User text is added only to the
request-local model prompt, not shared session history.

[Direct composer tests](../../tests/test_mnerys_gateway_turn_composer.py)
verify lifecycle and pre-I/O rejection. Real Web chat remains on its current
fully featured compatibility path, which calls admitted Agent execution but
does not yet use the direct composer. Tool-enabled composition, approval
continuity, attachment/document input, durable writes and typed turn
events are pending. See the [M2.1 exit gate](M2_1_EXIT_GATE.md).

## M2.1 incremental implementation: admitted terminal-response persistence

**Implemented only as an owner check around the inherited Web writer.**
[GatewayTurns](../../src/gateway_turns.py) now calls the
[admitted assistant persistence service](../../src/gateway_assistant_persistence.py)
for five terminal-response save sites, checking the issued session, loaded
owner, caller context and claimed session ID before invoking the existing
[legacy save helper](../../routes/chat_helpers.py). Both successful and
terminal failure metadata, incognito handling and saved message IDs retain
their previous path. This is **not** a new persistence engine, atomic
user+assistant commit or production revision-fenced transaction.

Cancelled/disconnected partial responses, synchronous Chat writes and
the new direct in-process text composer still have their previous
persistence behavior. [The writer inventory](SESSION_WRITER_INVENTORY.md)
and [M2.1 exit gate](M2_1_EXIT_GATE.md) remain authoritative on
these incomplete migration gates. See
[admitted save tests](../../tests/test_mnerys_gateway_assistant_persistence.py).

## M2.1 incremental implementation: admitted interruption/terminal lifecycle

**Implemented as a per-stream compatibility fix; durable lifecycle remains
planned.** In addition to the five admitted final assistant saves, both
[Web Chat/Agent interrupted partials](../../routes/chat_routes.py) now
pass through [Gateway admission](../../src/gateway_assistant_persistence.py)
before invoking the [same inherited session writer](../../routes/chat_helpers.py).
Saved terminal responses block late cancellation partials and duplicate DONE
saves. Cancellation cleanup survives an underlying writer failure.
Original fallback/model provenance and stopped-thinking metadata remain
untouched.

These checks **do not** provide transactional, cross-worker exactly-once
message writes, a new event log or a complete direct composer. See the
[M2.1 exit gate](M2_1_EXIT_GATE.md),
[writer inventory](SESSION_WRITER_INVENTORY.md), and
[real-stream cancellation tests](../../tests/test_foreground_model_routing.py).

## M2.1 incremental implementation: filtered process-local typed events

**Implemented only for the direct, text-only, tools-disabled composer.**
The [Gateway facade](../../src/gateway_turns.py) can now expose
[TypedTurnDelivery](../../src/gateway_turn_events.py) with a stream of
filtered internal events and a mutable completion snapshot. The inherited
Agent Loop still outputs SSE; a bounded compatibility translator converts
only visible message deltas, explicit final text, integer usage and
generic terminal states, filtering tool/approval/reasoning/provider raw
payloads. Duplicate DONE cannot turn one success into two terminal events;
provider failures cannot become successes after late DONE; incomplete EOF
fails closed. Cancellation and close propagate to the existing model-loop
generator, preserving its original bridge and contract context lifecycle.

This **does not implement** the proposed server-issued `turn_id`, durable
`seq`, restart-safe replay, authenticated event API or native Agent Core
event publisher. Production Web Chat and Agent continue to use the original
SSE transport and response writers unchanged. See the
[transitional mapping and security boundaries](M2_1_TYPED_TURN_EVENTS.md),
[typed event regression tests](../../tests/test_mnerys_gateway_turn_events.py)
and [direct composer tests](../../tests/test_mnerys_gateway_turn_composer.py).

## M2.1 incremental implementation: native, opt-in Core completion

**Implemented as a narrow internal Core emission seam.** The
[Agent Loop](../../src/agent_loop.py) now emits an immutable
[CoreTerminalEvent](../../src/agent_core_events.py) at its eleven original
completion sites when an admitted direct text-turn requests native terminal
delivery. The [evidence/completion gate](../../src/agent_runtime/completion.py)
and [teacher gate](../../src/teacher_escalation.py) preserve the native
completion signal and still own final answer filtering, terminal precedence
and cancellation cleanup. The tools-disabled direct mode suppresses teacher
child escalation; the existing Web request mode continues to emit the
**same SSE completion bytes** and retains existing teacher behavior.

The [Gateway execution boundary](../../src/gateway_agent_execution.py)
rejects native mode unless admission/owner/authority match and the final
ToolPolicy disables all native and MCP tools without an external bridge.
The [direct event adapter](../../src/gateway_turn_events.py) consumes native
completion directly while still translating legacy SSE deltas/usage/errors.
This is **not** native emission for all event families or the promised
persistent, versioned TurnEvent protocol. See
[the transitional event contract](M2_1_TYPED_TURN_EVENTS.md),
[real Core emission tests](../../tests/test_mnerys_agent_core_native_terminal.py)
and [M2.1 exit gate](M2_1_EXIT_GATE.md).

## M2.1 incremental implementation: post-evidence native event families

**Implemented for admitted, tools-disabled direct text turns only.** The
[Agent Core completion gate](../../src/agent_runtime/completion.py)
emits [native visible text, safe final token metrics and redacted failure
events](../../src/agent_core_events.py) after applying its existing
evidence/disclosure checks. The [teacher gate](../../src/teacher_escalation.py)
passes these typed events without starting an effectful teacher child.
The [Gateway event adapter](../../src/gateway_turn_events.py) consumes
them directly. The model/provider ingestion path and all tool/approval
events still use legacy SSE internally; the production Web transport
still receives the original SSE without opting into native output.

Native payloads omit provider diagnostics, raw tool data, reasoning and
credentials. The restricted opt-in does not grant tools. The local sequence
is not durable, and the composer still does not persist the conversation.
See [transitional event mapping](M2_1_TYPED_TURN_EVENTS.md) and
[the M2.1 exit gate](M2_1_EXIT_GATE.md).

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

## M4 stabilization transcript invariants (implemented)

The composed turn pipeline now carries these tested invariants (suite:
`tests/test_mnerys_turn_lifecycle.py`, disposable SQLite and PostgreSQL 17):

- One confirmed user message and one assistant message per successful turn;
  the user receipt check fails the turn before the provider runs, so a
  refused user append can never orphan an assistant reply.
- A client that disconnects mid-turn never loses its input or the reply: the
  detached run finishes server-side and the transcript is complete.
- Terminal outcomes are exactly-once per turn: duplicate terminal events in
  the provider stream or a replay cannot create a second assistant row.
- Cancellation terminates exactly the targeted turn id; sibling turns are
  untouched.
- A service restart reconciles durable `running` turn records at startup
  (marked failed with a terminal timestamp); already-terminal records are
  never touched, and an unavailable database cannot break the reconciliation.
- Owner security checks reuse the application-bound AuthManager; no runtime
  re-instantiation of the auth store mid-turn.
