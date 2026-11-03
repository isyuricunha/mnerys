# Proposed Gateway / Agent Core contract (M1)

**Status: proposal only; NOT an implemented API or schema.**
Related: [M1 source audit](AGENT_CORE_GATEWAY_M1_AUDIT.md), [Mnerys roadmap](../ROADMAP.md).

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

## M1 acceptance matrix — future tests

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

**Status:** contract proposed. None of these new routes or durable semantics should be described as implemented until code and tests prove them.
