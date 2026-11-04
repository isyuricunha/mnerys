# M2.1 exit gate — one reusable admitted Agent turn

**Status: PARTIAL — direct in-process Gateway execution is implemented and tested with a synthetic model; complete turn composition and real adapter acceptance remain open.**

This is the bounded, verifiable completion checklist for [M2.1 in the implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md#m21--finish-the-internal-authority-and-turn-boundary), under the [M1 design contract](GATEWAY_AGENT_CONTRACT.md). It replaces open-ended extraction targets with acceptance criteria. An item is complete only when its implementation and negative tests can be identified in the current source.

## Verified now

- **Session identity and admission:** [GatewayAdmission](../../src/gateway_admission.py) validates the server-issued principal against an owner-scoped session lookup; browser, scoped bearer and single-user cases have separate checks. The proof is **in-process**, not an externally usable token.
- **Turn preparation components:** [GatewayTurns](../../src/gateway_turns.py) composes admitted model fallback/context-policy decisions, tool inventory, capability/tool selection, immutable contracts, account/global restrictions, approvals and response jobs. [Context ports](M2_CONTEXT_SERVICE_PORTS.md) reuse legacy memory, attachment and persistence providers without cloning them.
- **Direct Agent execution:** [the admitted execution service](../../src/gateway_agent_execution.py) now binds the final RequestAuthority and ToolPolicy to an issued session **before invoking the existing** [TurnService and Agent Loop](../../src/turn_service.py). The selected model and provider headers come from the loaded session, not runtime overrides. Account/delegated denials can only narrow tool access. A forged/foreign admission, authority or model is rejected. This service imports no HTTP route or FastAPI component.
- **Lifecycle proof:** [isolated execution tests](../../tests/test_mnerys_gateway_agent_execution.py) start a synthetic, no-network turn through **admission → trusted authority → the inherited Agent Loop → direct event delivery**, entirely from Python without the Web router. Completion, early close, provider error and cancellation restore the original bridge and tool-contract context; the outer Gateway facade explicitly closes its inner generator.
- **Direct native, filtered text and terminal signals:** [GatewayTurns.open_composed_text_events](../../src/gateway_turns.py) offers the direct composer (tools-disabled by default) as process-local [typed events and an outcome](M2_1_TYPED_TURN_EVENTS.md). The [Agent Core](../../src/agent_loop.py) emits native completion objects; the [completion/evidence gate](../../src/agent_runtime/completion.py) now emits visible answer text, safe final metrics and generic provider/agent failure objects after security filtering. Separately opted-in admitted execution now emits [filtered native tool metadata and sealed approval references](M2_1_TYPED_TURN_EVENTS.md#native-tool-and-approval-metadata); provider ingress and other families remain inherited SSE, with no durable sequence or Web cutover.
- **Existing Web response writes now admitted:** [five Chat/Agent completion and terminal-failure saves plus both stopped-partial branches](../../routes/chat_routes.py) pass through the [Gateway assistant-save boundary](../../src/gateway_assistant_persistence.py), which verifies admission, session and owner before calling the unchanged inherited writers. Terminal completion now prevents per-stream duplicate DONE and save-after-cancel writes; incognito, metadata cleanup, fallback attribution, DB IDs and cancellation behavior remain in their existing pipelines. The synchronous route and direct composer are **not** migrated. Neither final nor partial saves use atomic/fenced whole-turn transactions.
- **One-call text-input composer (opt-in text-only persistence):** [the direct turn composer](../../src/gateway_turn_composer.py) now assembles context through injected [trusted ports](../../src/gateway_context_ports.py), applies admitted account policy, seals a tools-disabled authority by default, and enters the existing Agent Loop through the same execution service. It validates admission and account limits **before any context I/O**. The default mode appends the preprocessed input only to the model prompt and writes no conversation messages; the explicit trusted, tools-disabled text profile writes the user through the original context port and the completed answer through the inherited admitted saver. Its optional server-owned native tool profile now composes the existing inventory, capability/intent services, bound authority and immutable contract before entering the same Agent Loop; [composed tool tests](../../tests/test_mnerys_gateway_composed_tools.py) exercise an actual loop/immutable-read handoff using synthetic model/tool results. Native approval controls now resume sealed actions through this composer and the original dispatcher, with an explicit waiting outcome and no button-derived grants. Context preparation precedes one-use consumption; denial executes no tools. [Approval acceptance tests](../../tests/test_mnerys_gateway_composed_approvals.py) cover request/approve/resume, replay, scope checks, failures and cancellation. [Workspace-bound native approval tests](../../tests/test_mnerys_gateway_workspace_approvals.py) now cover a server-selected workspace matching the admitted session, the inherited Loop/dispatcher, policy changes and approval replay. MCP, document-bound and filesystem-resource approvals, and full Web workspace parity remain unsupported. [Direct functional tests](../../tests/test_mnerys_gateway_turn_composer.py) cover browser/bearer/single-user identity, invalid/foreign sessions, account veto, no-write behavior, disabled tools and stream lifecycle. This is intentionally **not** the general production composer yet.

These checks prove an internal executable boundary, **not** a fully independent user-facing Gateway or a real-model production run.

## Remaining M2.1 completion gates

1. **One admitted turn composer — PARTIAL, acceptance OPEN.** A single-call, **text-input** direct composer is implemented and tested, remaining non-persisting by default with an opt-in trusted text persistence profile. Tools remain disabled by default; a server-selected native surface now traverses the original inventory, policy, authority, contract and execution services. The production Web stream now validates its five terminal saves and both stopped-partial saves through the admitted Gateway, and prevents duplicates within the same completed stream. The inherited writers remain unchanged. The direct composer now offers opt-in persistence for an admitted, tools-disabled text turn: the shared context writer saves the user message, and the existing admitted assistant saver writes the completed reply before the typed completion acknowledgement. Failure to write the reply returns a generic persistence failure. This is a sequential pair of legacy writes, not a fenced or atomic whole-turn transaction. An optional server-owned interrupted writer now retains only filtered visible partials after failure, cancellation or early close, using the existing admitted stopped-response writer. Completion frames are withheld until the assistant write succeeds; ambiguous failures are never blindly retried. Expand this existing application operation to the actual Web feature set — owner-bound tool inventory/contract, active document/email, uploads, model fallback, full UI-bound approval continuation and message/response persistence — without duplicating logic. Do not allow a remote caller to deserialize a RequestAuthority, policy, principal or bridge. Today, [the Web route](../../routes/chat_routes.py) still orchestrates production features before it calls Gateway execution.
2. **Transport-neutral turn result and lifecycle — PARTIAL, acceptance OPEN.** The direct no-tools text composer returns [filtered, typed process-local events and an in-memory outcome](M2_1_TYPED_TURN_EVENTS.md). The Agent Core emits native completion, post-gate answer text, safe final metrics and sanitized failures in that restricted mode; provider ingress and tool/approval/other events still arrive as SSE. Production Web remains on its unchanged legacy SSE path. Native tool start/progress/output metadata and sealed approval observations are implemented behind a separate admitted runtime option. Extend the remaining Core event families and full client tool/approval rendering, preserve exact-run stop/finish/reconnect semantics, response persistence/partial saves and approval continuity, then move serialization exclusively to adapters. Durable replay and cross-process transactional fencing belong to later [M2 storage work](SESSION_REVISION_MIGRATION.md); do **not** claim them as part of this in-process gate.
3. **Web compatibility and direct-client parity — OPEN.** Route the existing Web chat path through the single composer, then add no-HTTP full-turn tests and Web-versus-direct parity for browser/bearer admission, tools, Plan/Compare, incognito, attachment/document inputs, approval continuations, model fallback, interruption and error persistence. At least one controlled **real model** turn and full CI are required before accepting the cutover; synthetic model fixtures alone are insufficient.
4. **Evidence and scope audit — OPEN.** Audit the final diff against the [session-writer inventory](SESSION_WRITER_INVENTORY.md) and [M1 contract regression baseline](M1_CONTRACT_TEST_BASELINE.md), validate that no new tool grants, user/session writes, remote entry points or secret-bearing examples slipped in, and update the roadmap. Deployment is a separate approved step with backup and rollback checks.

## Deliberately out of scope for M2.1

- A publicly reachable authenticated Gateway API and independently installable CLI/TUI clients.
- Durable cross-process TurnRecord/TurnEvent storage, revision-fenced writers and replay after server restart.
- Migrating SQLite or PostgreSQL production databases; replacing the original Agent Loop or dispatcher.

Follow the ordered [M2 execution plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) for those milestones. Do not declare M2.1 complete until **all four open gates** have verified evidence.

## Verification for this execution slice

The latest local regression batches passed **948 focused** (including the corrected required-read CI regression), **191 routing/streaming**, **159 authority/approval**, **160 peripheral**, **1,129 contract compatibility** and **148 external schema/tool-edit** tests. The groups overlap; these are not unique test totals. The focused set includes the prior CI failure regressions and synthetic direct Gateway execution. Existing SQLAlchemy and pytest-asyncio deprecation/configuration warnings remain. The hosted CI run for the published ten-commit batch found 12 failures in the shard-4 [required-read AST isolation tests](../../tests/test_required_read_operation.py): the extracted terminal branch lacked the enclosing Agent Loop's new `native_terminal_events` default and `completed_core_event` reference. The test-only harness now supplies both dependencies, preserving the production branch and its original SSE assertions. All 77 targeted required-read and native-event tests passed locally after the correction. A new hosted CI run is still required; do not claim the published batch passed. Live model and preview acceptance remain outstanding.

## Direct text persistence integration — November 4, 2026

The existing direct composer now accepts a server-created
[`TrustedTextPersistence`](../../src/gateway_text_persistence.py) through
`GatewayTurns.open_composed_text_events`. The normal direct profile remains
read-only for conversation messages. With the trusted profile, the context
algorithm saves the user text through its existing context port; the typed
completion wrapper then calls the original admitted assistant saver exactly
once before yielding `turn.completed`. A writer exception produces a generic
`persistence_error` terminal event instead. Malformed text preprocessing is
rejected before the user append; tools, approval controls and untrusted saver
objects are rejected before context I/O.

[Isolated persistence acceptance](../../tests/test_mnerys_gateway_text_persistence.py)
tests complete and repeated turns, double-DONE suppression, saver failure,
provider error, interrupted streams, foreign ownership and unsupported inputs.
It uses a synthetic model and in-memory legacy-port substitute only. The
two writes remain separate, with no durable receipt, cross-process fencing,
durable partial/error receipts, real database integration or Web cutover. This
slice does **not** close the M2.1 persistence or parity acceptance gates.

### Interrupted-response compatibility follow-up — November 4, 2026

The same [direct text persistence wrapper](../../src/gateway_text_persistence.py)
now optionally reuses the admitted legacy interrupted writer for filtered
partial text after client close, cancellation, incomplete stream or provider
failure. It never retries an ambiguous write. The final message event is
buffered until the existing assistant saver succeeds, closing a gap where a
consumer could observe final text before persistence. On final-save error,
only a sanitized failure is delivered, without a second partial append.
Late session-owner changes are rejected before either assistant writer runs.

[Updated isolated acceptance](../../tests/test_mnerys_gateway_text_persistence.py)
covers these states and continuing a *new* turn from the saved stopped
partial. It is not replay of the previous run and does not implement durable
idempotency or restart-safe resumes. All M2.1 parity, real-model, production SQL migration
and atomic turn storage acceptance gates remain open.

### Disposable SQLite and PostgreSQL 17 writer acceptance — November 4, 2026

The [cross-backend integration suite](../../tests/test_mnerys_gateway_text_sqlite_integration.py)
now uses disposable SQLite and PostgreSQL 17 databases with the inherited SQLAlchemy
`Session` and `ChatMessage` tables, the real `SessionManager._persist_message`
implementation, and the original assistant/partial save functions. The
test redirects only the legacy timestamp update and optional SFT trace hook
to isolated test behavior. It verifies durable message rows and counters,
both terminal and stopped saves, deleted sessions, and deterministic insert
rejection. No configured Mnerys database is opened.

The initial test exposed a **silent write failure**: the legacy manager can
return `False` without raising, allowing a completed event for an unwritten
message. The opt-in direct composer now requires a fresh post-commit
`_db_id` on its newly appended user record before starting the model, and
requires a new assistant record receipt before emitting completion or
acknowledging a stopped partial. Unconfirmed user writes yield a generic
`persistence_error` without running the model. This is a process-local
compatibility check, not an independent transaction read or durable receipt.

A separate test exposed stale in-memory session ownership after database
ownership transfer. The inherited `SessionManager._persist_message` now
compares the cached owner with the current SQL owner before insertion and
fails closed on mismatch; the cache reverts the append. This narrows a
specific stale-session window but **does not protect against an owner transfer
racing between that check and commit**. Revision/incarnation fencing remains
inactive. Both SQLite and PostgreSQL 17 passed the same seven isolated
direct-turn cases; five more negative tests verify that non-isolated PostgreSQL
connection strings are refused before connecting. The local PostgreSQL harness
uses a uniquely named Docker container with tmpfs data, a loopback-only random
port, and dedicated test credentials. The fixture verifies the authenticated
database, role, and server version before schema creation. The PostgreSQL 17 CI
harness creates a separate test database/role for this suite.

No production DB migration, Web cutover, durable fencing, or release approval
is implied.

### Session-bound native approval continuation — November 4, 2026

[The direct composer](../../src/gateway_turn_composer.py) now optionally
accepts a **server-selected session workspace** through the trusted settings
object. Before any I/O-capable context ports run, it requires an absolute,
existing directory that canonically matches the loaded admitted session's
`cwd`. The selected canonical path is passed consistently to the inherited
Gateway policy, request-authority construction and Agent Loop. A direct
approval's existing sealed workspace must match the selected path; both
`ExactToolApproval.matches` and the original request authority validate the
binding before any approval is consumed.

Only the existing **native backend** approval subset is admitted. The direct
profile still rejects cards requiring document IDs/digests/versions, filesystem
resource operations, subprocess or browser bindings, and MCP. A trusted
workspace value does not add tool grants or weaken policy, offered-schema,
ownership or approval-scope checks. The path is checked again after async
context preparation and before consuming an *allow* decision. Changing the
workspace, retargeting its symlink, disabling the tool or revoking account
privileges all leave an attempted allow unconsumed. An explicit **deny** can
still close the pending card even if its workspace has since disappeared:
denial needs no tool, path resolution, context processing or new authority.

[Acceptance tests](../../tests/test_mnerys_gateway_workspace_approvals.py)
cover a real inherited Agent Loop and dispatcher request → approval → single
native effect, replay denial, owner/session/workspace binding, changed context,
current policy, filesystem symlink retarget and cancellation. Only temporary
directories and synthetic providers are used. This is **not** active-document
support, filesystem operation authorization, an independently authenticated
Gateway or Web parity. A workspace check is not a filesystem handle or
atomic path-incarnation guarantee; the underlying dispatcher and resource
identity checks remain authoritative. See the
[Python path-resolution semantics](https://docs.python.org/3/library/os.path.html#os.path.realpath).
