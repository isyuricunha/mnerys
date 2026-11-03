# M1 — Agent runtime audit and extraction plan

Status: **audit completed; internal TurnService, session admission, ownership lookup and account privilege-limit seams partially implemented; complete Gateway contract and durable state NOT implemented.**
Scope: source inspection and isolated unit/regression checks at the beginning of Mnerys milestone M1. That original audit changed no runtime, database, authentication, public API or deployment. Later internal extractions are tracked separately in the [implementation contract](GATEWAY_AGENT_CONTRACT.md).

Read this with [Mnerys roadmap](../ROADMAP.md) and the [proposed shared contract](GATEWAY_AGENT_CONTRACT.md). File paths and line ranges below refer to the audited source tree, not to an installed release; recheck after refactoring.

## Executive decision

**Extract orchestration, do not rewrite the agent.** The imported system already has a working Python multi-round agent, request-scoped authority, resource binding, tool policy and approval logic, model routing, memory providers, MCP transports, persistent session records and background scheduling. A premature independent CLI or TUI agent engine would bypass these protections and fragment sessions.

The main technical issue is **where composition lives**: the Web chat route both accepts HTTP input and assembles turn context, model choices, tool policy, approval continuation, agent execution, streaming/rendering events and persistence. The first engineering seam should be a transport-neutral TurnService that **calls the existing hardened agent loop and tool dispatcher**, with all clients going through a shared Gateway authorization layer.

## Findings — source-backed inventory

| Area | Existing implementation and source evidence | Assessment |
| --- | --- | --- |
| Web composition | app.py creates FastAPI and wires singleton managers/routes (around lines 259, 723–748, 826–831); routes/chat_routes.py exposes POST /api/chat_stream (around 2468), constructs policy/context and calls stream_agent_loop (around 4612). | **Inherited, functioning but coupled** to HTTP, form parsing, SSE, browser state and process-local managers. |
| Agent execution | src/agent_loop.py:stream_agent_loop (around 21029) implements the multi-round streaming loop. routes/chat_routes.py wraps it in a request-local execution bridge (around 853) and performs persistence/render ownership in the stream generator. | **Reuse** the loop; extract initiation, event conversion and finalization into a service facade before changing behavior. The file is very large and is not itself a public core API. |
| Turn selection | src/turn_contract.py:TurnContract/resolve_turn_contract (around 6279–6353) and src/tool_policy.py:ToolPolicy (around 166) select/restrict exposed tools. Existing [turn contract notes](agents/AGENT_TURN_CONTRACT.md) specify required/offered/executable scopes. | **Reuse as selection and restriction**, never as authorization. |
| Security/authority | src/agent_runtime/authority.py:RequestAuthority/create_request_authority/bind_request_authority (around 110, 364, 439); src/tool_execution.py:execute_tool_block (around 1631–1717) requires a security context and checks authority, exact operation, policy and turn scope before dispatch. Resource/backend/process/browser bindings follow. | **Protect this execution boundary.** New clients must not directly invoke arbitrary tools or construct trusted request authority. |
| Approval and continuation | src/tool_approvals.py:ToolApprovalStore (around 352) holds sealed, digest-bound exact actions with TTL; routes/chat_routes.py consumes approval in the chat-stream route (around 2820–2889). | **Reuse semantics; redesign storage.** Pending approvals are process-local. Later phases require durable, owner-bound decisions and restart-safe replay. |
| Session persistence | core/session_manager.py:SessionManager (around 65) reads/writes core.database session/message records through SessionLocal (around 241, 559, 615). routes/session_routes.py includes ownership checks and CRUD/history endpoints. | **Reuse data and model**, introduce owner-scoped repository interface and avoid a new, incompatible session store. In-memory cached session objects are not a multi-process consistency contract. |
| Stream lifecycle | src/agent_runs.py manages detached per-session tasks and SSE replay with a module-level _RUNS dict (around 42, 187, 212). routes/chat_routes.py:resume/stop/finish (around 5009–5042) use run IDs and verify session ownership. | **Functional for single-process reconnect**, **NOT durable across restart**; current buffer replays from the start, with no universal per-event cursor contract. Must not promise restart recovery yet. |
| Event/recovery evidence | src/agent_runtime/journal.py and effect_log.py provide action/effect evidence and some durable effect recovery; src/event_bus.py uses an in-process scheduler reference. | Reuse effect-evidence concepts; **do not conflate effect log with a persisted stream of client-visible turn events**. |
| Model providers | src/llm_core.py, src/endpoint_resolver.py, src/foreground_model_routing.py, src/model_context.py and routes/model_routes.py implement existing model resolution and provider behavior. | **Reuse behind a model service interface**; do not force remote CLI to import web app and provider dependencies. |
| MCP/tools | src/mcp_manager.py:McpManager (around 147, 561, 688) connects server transports and calls tools; src/builtin_mcp.py registers optional built-ins; src/tool_execution.py dispatches local/remote operations and supports an inherited client execution bridge. | **Reuse adapter and dispatcher**; keep a server-owned allowlist, resource identity and tool audit. Do not treat external bridge URLs or client-supplied tool descriptions as permission grants. |
| Memory | src/memory_provider.py:MemoryProvider/NativeMemoryProvider/MemoryProviderRegistry (around 33, 96, 258), services/memory and routes/memory expose persistent/semantic memory behaviors. | Provider abstraction is a valuable extraction point; audit ownership, indexing and context injection separately from CLI/TUI rendering. |
| Skills | services/memory/skills.py, services/skills, routes/skills_routes.py combine lifecycle, audit, execution and HTTP management. | Preserve lifecycle and trust distinctions; make skills a Core capability, not client-managed instructions. |
| Automation | src/task_scheduler.py:TaskScheduler (around 388, 1982), src/background_tool_jobs.py and core.database ScheduledTask/TaskRun/BackgroundToolJob already support scheduled and durable records. Authority sealing exists in src/agent_runtime/authority.py:seal_task_authority (around 519). | **Keep scheduler server-side.** Reuse shared TurnService with the same authorization policy; test task ownership and restart behavior before unification. |
| Authentication and scopes | core/auth.py:AuthManager and src/auth_helpers.py implement existing user identity/API-token handling; routes/chat_routes.py checks chat scope and session owner; routes/session_routes.py verifies ownership for many operations. | **Gateway must authenticate every transport** and authorize per user, session, agent, operation and resource. Existing route guards must be tested and retained during extraction. |
| CLI/TUI | pyproject.toml presently contains pytest settings, not a distributable Mnerys console entry point; setup.py is the inherited first-time installer. An inherited odysseus-tui client-runtime bridge and tests/cli development helpers exist, but these do **not** establish a shipped standalone Mnerys CLI/TUI. | **Planned only.** Define a light remote client package independently of server runtime and local-execution optional dependencies. |

## Main coupling and risk register

**P0 — authority regression.** Existing permission checks are spread across route admission, tool selection, request authority, resource binding, exact approval and dispatcher. Extracting just the agent loop without the other layers can enable bypasses. Default deny when authorization context is missing; never create authority from untrusted serialized client fields.

**P0 — approval integrity.** Pending approval state is currently held in the process. On restart, do not reconstruct a grant from UI card data or historical model output. Any durable approval must bind owner, session, originating turn/run, sealed operation, workspace/resource identity, scope, expiry and single-use consumption.

**P0 — session/history correctness.** A single process currently caches Session objects; separate CLI/Web workers may race on writes, final messages, stop and replacement. Introduce one active writer/turn per session initially, version/fencing checks and owner-scoped access. Preserve existing sessions and encryption material through migrations.

**P1 — event durability.** Current detached SSE replay is not restart-safe and replays an in-memory buffer. Versioned event IDs, a bounded persisted event journal and stable resume cursor are prerequisites for dependable cross-client continuity. Existing stored assistant messages are not a complete live event journal.

**P1 — startup side effects.** app.py creates managers, installs routes and starts background services. Remote CLI must not import app.py or instantiate scheduler/MCP workers just to submit a prompt.

**P1 — local/remote filesystem and browser identity.** The inherited TUI bridge can route host operations. A new local mode needs explicit user ownership, workspace confinement, connector identity and audited approvals; Tailscale access alone does not authorize a tool.

**P1 — persistence boundaries.** SQLite is the default, PostgreSQL 17 is a supported primary DB alternative, but memory/vector, auth files, scheduler and auxiliary stores are not all governed by the primary database. Design a backend-neutral repository and migration, not a single-URL assumption.

## Implementation sequence (proposed; not yet shipped)

1. **Freeze contract tests** for HTTP admission, effective principal, session ownership, model/tool selection, approval deny/allow, tool dispatch, run cancellation, message persistence and event ordering; add explicit multi-client negative cases.
2. **Introduce typed, transport-neutral interfaces**: Principal, SessionRepository, TurnService, TurnEventStore, ApprovalRepository, AgentRuntimeAdapter, ToolExecutionGateway, ModelResolver and MemoryProvider. Define schema and compatibility policy in [contract proposal](GATEWAY_AGENT_CONTRACT.md).
3. **Extract turn orchestration incrementally** from routes/chat_routes.py into TurnService while delegating to the same stream_agent_loop and execute_tool_block path. Keep the legacy Web endpoints as adapters; run behavioral parity tests before switching the default path.
4. **Durable run/approval control plane**: transactional owner-scoped turn records, event sequence, resume cursors, single active turn fencing, cancellation and atomic approval consumption. Support SQLite and PostgreSQL with reviewed migrations, backup and restore tests. Do not promise exactly-once effects without explicit idempotency and reconciliation.
5. **Adapt scheduler, skills and background jobs** to use the same Gateway/Core authority; preserve existing task data and isolation. Deprecate duplicate orchestration only after parity tests.
6. **Only then deliver clients**: lightweight, globally installable mnerys remote CLI; optional local execution; first-party TUI reusing the Gateway events; Web continues to share the same sessions and policies.

## Validation performed for this audit

- Source files and entry points inspected **read-only**. No private production data or credentials read.
- Isolated regression run in a temporary data directory, with the validated project Python environment: **1,245 tests passed** across agent runs, request authority, tool approvals, tool policy, turn contract, memory provider, agent runtime context and session manager. This is baseline confidence, **not** a live CLI/Gateway end-to-end proof.
- No infrastructure configuration, database schema, live preview runtime, public API or tool policy changed in this workstream.

## Milestone status and acceptance gates

**M1 subtask complete:** inventory, ownership/security risks and extraction order documented.

**M1 milestone remains incomplete** until the shared contract is implemented and exercised by at least two transport adapters without changing policy semantics. Required evidence:
- Authorized owner can create/resume/cancel a turn; another owner cannot access it.
- A tool cannot execute without server-owned request authority, even through CLI, TUI, MCP or background automation.
- Approval is digest-bound, single-consumption, non-transferable and cannot be replayed across a restart.
- Stream event ordering and resume work with explicit cursor semantics.
- Existing Web behavior, session/message persistence and PostgreSQL/SQLite compatibility remain valid.
- Deploy/rollback and actual authenticated workflow smoke tests pass without leaking private data.

Keep M2–M4 marked **planned** until their independent acceptance gates pass.
