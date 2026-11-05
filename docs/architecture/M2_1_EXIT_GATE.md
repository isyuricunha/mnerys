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
- **One-call text-input composer (opt-in text-only persistence):** [the direct turn composer](../../src/gateway_turn_composer.py) now assembles context through injected [trusted ports](../../src/gateway_context_ports.py), applies admitted account policy, seals a tools-disabled authority by default, and enters the existing Agent Loop through the same execution service. It validates admission and account limits **before any context I/O**. The default mode appends the preprocessed input only to the model prompt and writes no conversation messages; the explicit trusted, tools-disabled text profile writes the user through the original context port and the completed answer through the inherited admitted saver. Its optional server-owned native tool profile now composes the existing inventory, capability/intent services, bound authority and immutable contract before entering the same Agent Loop; [composed tool tests](../../tests/test_mnerys_gateway_composed_tools.py) exercise an actual loop/immutable-read handoff using synthetic model/tool results. Native approval controls now resume sealed actions through this composer and the original dispatcher, with an explicit waiting outcome and no button-derived grants. Context preparation precedes one-use consumption; denial executes no tools. [Approval acceptance tests](../../tests/test_mnerys_gateway_composed_approvals.py) cover request/approve/resume, replay, scope checks, failures and cancellation. [Workspace-bound native approval tests](../../tests/test_mnerys_gateway_workspace_approvals.py) now cover a server-selected workspace matching the admitted session, the inherited Loop/dispatcher, policy changes and approval replay. MCP, remote/unbound filesystem approvals, process/browser resources and full Web workspace parity remain unsupported. A restricted, sealed native filesystem approval profile is described below. A separately verified, restricted active-document edit/update/suggest profile is described below. [Direct functional tests](../../tests/test_mnerys_gateway_turn_composer.py) cover browser/bearer/single-user identity, invalid/foreign sessions, account veto, no-write behavior, disabled tools and stream lifecycle. This is intentionally **not** the general production composer yet.

These checks prove an internal executable boundary, **not** a fully independent user-facing Gateway or a real-model production run.

## Remaining M2.1 completion gates

1. **One admitted turn composer — PARTIAL, acceptance OPEN.** A single-call, **text-input** direct composer is implemented and tested, remaining non-persisting by default with an opt-in trusted text persistence profile. Tools remain disabled by default; a server-selected native surface now traverses the original inventory, policy, authority, contract and execution services. The production Web stream now validates its five terminal saves and both stopped-partial saves through the admitted Gateway, and prevents duplicates within the same completed stream. The inherited writers remain unchanged. The direct composer now offers opt-in persistence for an admitted, tools-disabled text turn: the shared context writer saves the user message, and the existing admitted assistant saver writes the completed reply before the typed completion acknowledgement. Failure to write the reply returns a generic persistence failure. This is a sequential pair of legacy writes, not a fenced or atomic whole-turn transaction. An optional server-owned interrupted writer now retains only filtered visible partials after failure, cancellation or early close, using the existing admitted stopped-response writer. Completion frames are withheld until the assistant write succeeds; ambiguous failures are never blindly retried. Expand this existing application operation to the actual Web feature set — owner-bound tool inventory/contract, active document/email, rich uploads/multimodal input, full Web parity for model fallback, full UI-bound approval continuation and message/response persistence — without duplicating logic. Do not allow a remote caller to deserialize a RequestAuthority, policy, principal or bridge. Today, [the Web route](../../routes/chat_routes.py) still orchestrates production features before it calls Gateway execution.
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
workspace-only acceptance did not enable document IDs/digests/versions.
A separate active-document profile now allows only exact sealed
edit/update/suggest actions for the same owner/session. Native filesystem
resource approvals require the additional same-workspace sealed-operation
check described below; subprocess, browser and MCP bindings remain blocked. A trusted
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
directories and synthetic providers are used. That workspace-only suite is
separate from the active-document acceptance described below; neither grants
filesystem operation authorization nor an independently authenticated
Gateway or Web parity. A workspace check is not a filesystem handle or
atomic path-incarnation guarantee; the underlying dispatcher and resource
identity checks remain authoritative. See the
[Python path-resolution semantics](https://docs.python.org/3/library/os.path.html#os.path.realpath).

### Owner-scoped active-document continuation — November 4, 2026

The [read-only Gateway document resolver](../../src/gateway_document_context.py)
accepts an ID selected by a **trusted server adapter** and verifies it
against the existing SQL `Document` table before the direct Agent turn uses
the document. It requires the admitted user to be the document's explicit
owner; the document must be active, not archived and linked to the exact
admitted session, whose current SQL owner must also match the admission. Missing/legacy owners and cross-session editor handoffs
fail closed; unlike the inherited Web editor, the direct profile never
implicitly rebinds a document to another session.

A server-selected active document is passed as a frozen in-memory snapshot
into the inherited Agent Loop; its content is treated as context, **not**
authority. Exact approvals for native `edit_document`, `update_document`
and `suggest_document` are accepted only when the existing pending record
contains its fully sealed `BoundOwnedOperation`. Its document ID, version,
content SHA-256 digest, owner, session and resource observation must all
match the current SQL document. The original dispatcher still revalidates
the sealed owned resource and passes its expected document version/digest
to the document tool immediately before any effect.

Admission and account Agent permissions precede both workspace filesystem
lookups and document SQL reads. The document is read again after async
context preparation and before a one-use **allow** decision is consumed.
Changed content, version, ownership, session association, activation or
archive status leaves the original approval untouched. An explicit
**deny** still retires the card without requiring a document query.

[Cross-backend acceptance](../../tests/test_mnerys_gateway_document_approvals.py)
uses isolated SQLite and PostgreSQL 17 schemas, real SQLAlchemy document
rows, a real Agent Loop and dispatcher handoff with a **mocked final tool
effect**, and synthetic provider input. The existing PostgreSQL CI harness
now runs this suite in its dedicated M2.1 role/database. No configured
application DB, real document, external provider, production edit or
filesystem mutation is used. The tests cover owner/session isolation,
version/content changes, inactive/archive/deleted records, session ownership
transfer, early account denial, replay and context-time mutation.

This is **not** a transactional document-edit fence, full document-editor
parity, an authenticated external Gateway, or a Web cutover. There is
still a time-of-check/time-of-use gap between the Gateway read and the
original dispatcher; the inherited resource identity checks and document
writer's expected-version/digest guard remain authoritative, with no
new exactly-once guarantee. Full M2.1 exit gates remain open.

### Sealed native filesystem approval continuation — November 4, 2026

The [direct Gateway filesystem preflight](../../src/gateway_filesystem_context.py)
accepts **only** an existing `BoundFilesystemOperation` already attached
to the owner's one-use pending approval. An explicitly trusted workspace
must still resolve to the exact canonical directory selected by the admitted
session. Every sealed filesystem binding must use one identical
`FilesystemScope.WORKSPACE` root, owned by the admitted principal and
identical to the selected workspace. The original request ID, owner,
session and exact native operation must match the sealed resource. Inherited
or unavailable resource bindings fail closed: the Gateway never creates a
replacement root, infers an external path grant from tool text or accepts
the approval button as a new authority.

The inherited resource resolver checks the originally observed root and
file/ancestor identities, plus path confinement and control-plane path
exclusions. The Gateway validates the sealed operation **after current tool
policy checks and again after async context preparation**, immediately before
consuming an allow decision. Only that exact filesystem tool class enters
the routed contract on resume, even if the first run had offered additional
tool families such as `bash`. The original Agent Loop and dispatcher then
revalidate resource identity, actual account/tool-policy restrictions and
the one-use exact approval before an effect. Explicit `deny` remains
available without workspace access.

[Isolated acceptance tests](../../tests/test_mnerys_gateway_filesystem_approvals.py)
cover real inherited Agent Loop/dispatcher reads and writes of disposable
pytest files; other native operations are exercised at their sealed
handoff. Negative tests cover retargeted symlinks, replaced file/root/
ancestor objects, foreign resources, missing sealed operations, modified
request identity, cross-workspace context, current account/tool veto,
loss of workspace, cancellation, replay and an attempted multi-tool
continuation. The real public filesystem admin restriction is retained;
positive execution tests explicitly simulate an authorized administrator,
and a separate negative test proves a non-admin cannot write even after
approving a sealed action.

This is **not** OS-level inode/FD fencing, content snapshotting, sandbox
verification, production filesystem mutation, remote-resource support or
Web cutover. In-place content changes and filesystem races after validation
remain subject to the inherited tool/resolver's existing checks. No
session or event persistence semantics changed, and the M2.1 exit gate
remains open.

### Opt-in direct foreground model fallback — November 4, 2026

The [direct turn composer](../../src/gateway_turn_composer.py) can now
use the existing [owner-scoped foreground fallback policy](../../src/gateway_foreground_routes.py)
in the **same inherited Agent Loop**. This remains disabled by default
for direct Python callers. A trusted server adapter may opt in only with an
explicit nonempty, bounded set of **allowed fallback model names**; the
authenticated owner must separately have enabled the foreground fallback
preference. A direct turn never accepts a fallback list, endpoint, provider
header, model or route descriptor from untrusted request content.

The loaded session remains the primary model, endpoint and headers.
The existing resolver produces ordered candidates and safe, aligned
descriptors with the user's owner-scoped endpoint identities. The composer
checks the alternative model IDs against the adapter's allowed set,
and gives the resulting list to the already existing Agent Loop's
`fallbacks`, `route_descriptors`, `fallback_statuses`,
`fallback_on_empty` and `defer_context_shaping` options. The Loop
already applies per-candidate context/model budgets and chooses the first
eligible successful provider; no parallel executor or retry implementation
was added.

[Direct acceptance tests](../../tests/test_mnerys_gateway_direct_fallback.py)
include owner/bearer/single-user preference resolution, no-consent strict
mode, blocked/disallowed models, malformed server settings and early
account denials. A synthetic, no-network provider invocation exercises
HTTP 503 failover through the real Agent Loop and retained per-candidate
context. Ineligible status responses, visible first-provider output and
early stream close all retain the original fallback and cleanup rules.

This slice adds **no model usage cost** to the default direct profile,
no new provider credentials, no conversation writes, and no automatic
fallback on visible partial output. The typed direct consumer now receives a source-owned `model.route.selected` observation when the inherited candidate selector commits substantive fallback output. This is not a durable final-answer attribution receipt and exposes neither provider diagnostics nor URLs/headers.
The production Web route, real-provider acceptance, persisted answering
route attribution, durable replay and complete M2.1 cutover remain open.

### Owner-scoped direct text-upload context — November 4, 2026

The [restricted direct text attachment profile](../../src/gateway_attachment_context.py)
accepts up to four untrusted attachment IDs on a non-persisting,
**tools-disabled, no-workspace** direct text turn. Invalid selectors are
rejected before context I/O. The existing Web upload manifest port and
`UploadHandler.resolve_upload` check the admitted owner, canonical upload
root/path, metadata and actual SHA-256 bytes before processing. The
attachment snapshot (path, inode, size, hash, ID and metadata) is verified
again after asynchronous preprocessing. Missing, cross-owner, moved,
changed, too large or unsupported files fail closed.

Only bounded UTF-8 text files (`.txt`, `.md`, `.markdown`,
`.csv`, `.json`, `.log`) and corresponding text MIME types are currently
supported. Binary and invalid UTF-8 payloads are rejected. The inherited ChatHandler and document text extractor read
their content. A separately admitted `allow_attachment_preprocessing`
flag lets these files be parsed without re-enabling the existing URL,
YouTube, vision/VL or other tool-driven preprocessing. The default Web
and port signatures preserve the old behavior unless this flag is set.

The user-selected file content is appended as an explicitly guarded
`untrusted_context_message`, **not** merged into the direct user's
instruction or the system preface. The original request text alone
drives tool intent/authority. No uploaded paths are offered to the Agent
Loop as a new tool grant. The selected text upload remains read-only;
no user or assistant message is stored by this profile.

[Direct attachment acceptance](../../tests/test_mnerys_gateway_direct_attachments.py)
uses the original ChatHandler text processor with isolated temporary
upload files, owner-aware manifest fixtures and a fake model. It exercises
guard-marker injection, owner isolation, invalid IDs, size/format
constraints, symlinks and changed source bytes, no tool/persistence
escalation, default Web port compatibility and disabled URL/vision probes.

This does **not** complete general uploads, images/vision, audio,
PDF editor auto-creation, binary parsing, attachment-to-tool grants,
incognito persistence, transactional path fencing or real-provider/Web
parity. Those remain M2.1 acceptance work.

### Source-owned direct fallback route observations — November 4, 2026

The [original candidate selector](../../src/llm_core.py) now calls a
synchronous, internal observer **only when** an eligible fallback candidate
commits its first substantive answer or tool output. The
[inherited Agent Loop](../../src/agent_loop.py) forwards this as a
`CoreModelRouteEvent` through the existing completion and teacher wrappers.
The [typed Gateway adapter](../../src/gateway_turn_events.py) exposes a
restricted `model.route.selected` event containing only the selected model,
answering candidate model, candidate index and Agent round.

This observation is **not** reconstructed from provider-authored `fallback`
SSE. The model selector drops any provider-supplied `fallback` control frames;
it still preserves its original Web SSE fallback message when it actually
switches candidates. Default Web streaming and standard strict turns retain
their existing behavior. The native observation adds no tool permissions,
extra model calls or new execution engine.

[Isolated route tests](../../tests/test_mnerys_gateway_model_routes.py)
and [real-Loop direct fallback tests](../../tests/test_mnerys_gateway_direct_fallback.py)
cover eligible HTTP 503 fallback, noneligible errors, partial-output pinning,
forged control frames, redaction, native event ordering, duplicate terminals
and early close. Candidate selection is not a final response-save receipt
or proof of a provider's self-reported alias. Real provider acceptance,
durable model attribution, remote transport and complete M2.1 Web cutover
remain open. The M2.1 exit gate is still **PARTIAL**.

### One-run typed detach, replay and stop — November 4, 2026

The [admitted direct Gateway facade](../../src/gateway_turns.py) now offers
`open_composed_detached_text_events`, `resume_composed_detached_text_events`
and `stop_composed_detached_text_turn` for trusted Python adapters. A
server-issued session admission and owner-bound loaded session are checked
**before** registering or stopping a run. There is still no remote API
or independent client admission protocol.

The [original Web run manager](../../src/agent_runs.py) is reused:
one session-to-run registry, one drain task, one exact `run_id`, one
session-scoped replacement/stop rule, one finite in-memory buffer and the
same run-scoped replay cursor checks. Typed subscribers receive an
`TypedRunEvent` containing the original filtered `TurnEvent`, the opaque
run ID and a process-local cursor. They cannot attach to an SSE run,
and SSE consumers cannot interpret a typed buffer. Closing a subscriber
does **not** cancel its detached Agent turn; an explicit exact-run Stop
does, including when the task is cancelled before its first instruction.
An old run ID/cursor never stops or resumes its replacement.

The manager retains the terminal `turn.completed`, `turn.failed` or
`turn.awaiting_approval` event until the wrapped composer has finished
and its inherited text-persistence writer has returned. Therefore
a detached consumer cannot treat completion as durable before the
existing writer receipt check. A background stream exception is exposed
as a generic typed `stream_exception` failure, without raw exception text.
No duplicate Agent Loop, file/job writer, event registry or schema exists.

[Detached typed lifecycle tests](../../tests/test_mnerys_gateway_detached_typed_lifecycle.py)
exercise owner and forged-session denials, single model invocation despite
reconnect, cursor ordering, persisted completion, partial-save-on-stop,
stale run replacement, Web SSE separation, pre-entry cancellation,
unexpected provider exceptions and cleanup ordering.

**Boundaries:** replay lasts only as long as the existing process/run
buffer (with the inherited finite eviction window). It does not survive
a restart, provide cross-worker cursors, durable turn identity,
transactional persistence or remote authentication. The production Web
route remains on its original SSE/Agent path. Real-model and Web-versus-
direct parity acceptance remain required to close M2.1.

### Read-only plain-text Web SSE compatibility preview — November 4, 2026

A new [filtered Web compatibility adapter](../../src/gateway_web_compat_events.py)
serializes the already-typed, in-process Gateway events into the existing
browser's **strict plain-text** SSE vocabulary: `delta`, `final_response`,
`message_saved`, `metrics`, vetted Core-originated `fallback` and `[DONE]`.
It is **not invoked by the production Web routes** and does not recreate
authorization, tool dispatch, model execution, persistence or replay.

The trusted [Gateway facade](../../src/gateway_turns.py) exposes
`preview_composed_plain_text_web_sse` only for server-issued/owner-admitted,
**persisted, tools-disabled, attachment-free, no-workspace, no-active-editor**
text requests. A server-only `emit_message_saved_events` option in the
existing [legacy-backed persistence adapter](../../src/gateway_text_persistence.py)
emits the original assistant's fresh `_db_id` **after** its admitted writer
confirms the append and **before** `message.final` and `turn.completed`.
Default typed turns retain their original event sequence/shape. No
alternative writer or second database write was introduced.

The SSE serializer accepts only bounded text, approved numeric token usage,
validated message IDs and model IDs, sanitized error codes and monotonically
ordered typed events. Unsupported tool, approval and document events **fail
closed** instead of constructing incomplete browser action cards. Provider
diagnostics and forged model aliases are never forwarded. The in-process
detached replay mode can retain the existing exact run-scoped cursor with
no new event registry. For a late resume after the saved-message frame,
the original run manager itself verifies that the exact buffered run had
already emitted a receipt before the requested cursor; a cursor or
client assertion alone cannot establish one. No save acknowledgement is
exposed on a failed or unconfirmed assistant write.

[No-HTTP compatibility acceptance](../../tests/test_mnerys_gateway_web_compat_events.py)
covers the actual composer, original writer, identical browser-visible
plain text frames, missing/invalid/duplicate receipts, provider failure,
incomplete streams, forged fallback metadata, injected SSE delimiters,
unsafe tool/approval payloads and same-run cursor replay. This is the
**first constrained text serialization compatibility slice**, not
Web-versus-direct full-feature parity or proof that an HTTP client can
switch without regression. Rich tool UI, Plan/Compare, incognito, media,
documents, approvals, live-model acceptance and Web cutover remain open.

### Sanitized native-tool timeline for Web compatibility — November 4, 2026

The [existing no-HTTP Web serializer](../../src/gateway_web_compat_events.py)
now has a **separately opted-in, non-persisting status-only** tool profile.
The [admitted Gateway facade](../../src/gateway_turns.py) exposes
`preview_composed_tool_status_web_sse` for a server-issued, owner-bound,
**single `manage_notes` native surface** with no workspace, active document,
attachments or message persistence. The actual model/tool lifecycle still
runs through the same inherited Agent Loop, dispatcher, inventory, account
policy and request authority; this module is presentation, not another
tool executor or an execution grant.

Filtered Core `tool.started`, `tool.progress`, `tool.completed`,
`tool.failed` and `tool.blocked` observations are projected to the
browser's familiar `tool_start`, `tool_progress` and `tool_output` frames.
The status preview requires matching start/terminal events for exactly
one active tool. Commands, argument JSON, tool outputs, stdout/stderr,
URLs, document metadata and UI-effect keys are **never serialized**.
A blocked/failed action is never represented with a successful exit code.
This display is limited to the already supported Web tool timeline,
**not** complete native tool result parity.

A consumed, sealed native `approval.decided` may produce the browser's
`tool_approval_resolved` notification without the approval ID or any
new authority. Crucially, `approval.requested`, `tool.awaiting_approval`
and `turn.awaiting_approval` **fail closed**: an opaque pending ID is not
an owner-checked Web approval card, and the preview cannot fabricate
review buttons or continue an effect. The existing Web approval surface
and all of its persisted card metadata remain unchanged.

[No-HTTP tool timeline acceptance](../../tests/test_mnerys_gateway_web_tool_status.py)
covers the original composed tool authority and synthetic Agent Loop,
matching sanitized UI frames, failure/blocked status, one-use denial,
new sealed pending review, invalid tool names/frames, mismatched rounds,
concurrent/unfinished tool steps, and owner/adapter scoping. The tool
preview does not persist chats and has **not** been connected to the
production Web endpoint. General approval-card coverage for all tools,
editor side-effects, rich tool output and lifecycle/persistence parity
remain open M2.1 acceptance work.

### Exact sealed pending-review Web card preview — November 5, 2026

The existing [no-HTTP native tool timeline](../../src/gateway_web_compat_events.py)
now supports an **in-process, owner-bound, read-only** Web `ask_user` card
for an admitted `manage_notes` approval, without enabling arbitrary tools,
raw tool outputs, editor side effects or production Web migration.

A Core-native `approval.requested` event is **only a reference**. It cannot
become a button by itself. The trusted Gateway facade selects the same
[ToolApprovalStore](../../src/tool_approvals.py) already bound to the Agent
Loop and calls [read_admitted_pending_review](../../src/gateway_web_approval_cards.py).
The reader checks the issued admission, loaded session, owner, scope,
expiry, current pending ID, full immutable action seal (via the existing
`ExactToolApproval.matches`), exact input, original request authority,
allowed native tool and bounded complete Web display. No renderer data is
accepted as authority, and no pending card is consumed by displaying it.

The serializer holds the review reference across a matching native
`tool.awaiting_approval` observation and terminal
`turn.awaiting_approval`, **without** pretending that a gated tool executed.
It waits for the original composer/stream cleanup, rereads the *same*
unconsumed sealed record and checks its digest and public payload again
before producing the inherited Web `ask_user` card followed by `[DONE]`.
The review contains the complete input, original action digest prefix,
effects and existing task/session/deny choices; the default reason is
used because a custom original security reason is not stored in the
sealed record. Newline-containing input is JSON-escaped. A cancelled,
failed, superseded, expired, consumed, forged or changed proposal never
becomes an actionable card.

The original [admitted approval continuation](../../src/gateway_turn_composer.py)
still validates and consumes `approve`, `approve_task` or `deny` exactly
once under the original resource/authority policy; a displayed card does
**not** grant execution rights. Tests in
[sealed Web review acceptance](../../tests/test_mnerys_gateway_web_approval_cards.py)
cover complete cards, real composer and original store, one-use
continuation, denied execution, owner/session isolation, invalid action
seals, malicious SSE text, expiry during cleanup, cancellation,
falsified provider `ask_user` and absent/mismatched terminals.

**Still partial:** this is only an in-process/no-HTTP preview for the
single permitted native tool. It does not persist the original Web card,
authenticate a remote button, provide durable approval records/replay,
cover workspace/editor/browser tools, match custom security reasons,
or enable actual production Web UI routing. Real-model validation and
full Web Gateway parity remain exit-gate requirements.

### First guarded Web HTTP → single-composer cutover — November 5, 2026

The existing [`/api/chat_stream` route](../../routes/chat_routes.py)
now has a **disabled-by-default**, explicitly selected strict-text
routing branch. A trusted server must set
`MNERYS_M21_WEB_TEXT_ROUTING=1` and the already authenticated request
must declare `gateway_profile=text_v1` with
`no_memory=true` and `use_rag=false`. This is a controlled
compatibility experiment, **not** a default/production Web migration.

The fail-closed [eligibility gate](../../src/gateway_web_text_cutover.py)
runs only after the original Web session ownership check, account chat
limits, selected session/model resolution and owner-scoped foreground
policy. It refuses tools/Agent mode, approval controls, attachments,
active documents/email, selected notes, workspaces, incognito, compare,
research, RAG/memory, fallback, non-default thinking, custom model
settings, runtime bridges and unrecognized form/JSON features. An
unsupported request stays on the original Web branch rather than
silently discarding its options.

The [HTTP-only adapter](../../routes/gateway_text_adapter.py) binds the
**original** `LegacyChatContextPorts`, Gateway composer and admitted
assistant/interrupted writers, requesting a trusted `chat` history
marker instead of the direct Agent default. The existing
[run manager](../../src/agent_runs.py) provides detached lifecycle,
one-use run IDs, stop and bounded replay; the existing Web
`/api/chat/resume` checks a server-marked strict-Web typed run before
exposing its sanitized SSE. Internal typed runs from other adapters
**cannot** be replayed as Web sessions. The browser receives the
familiar mode/model headers and filtered text, assistant save receipt,
final and terminal frames. No new writer, model loop, event store or
authorization grant is introduced.

[Focused HTTP and route-selection acceptance](../../tests/test_mnerys_gateway_web_text_cutover.py)
covers a real ASGI transport with synthetic model and original
in-process writer, actual Web router dispatch into the guarded branch,
rejection/fallback for unsupported features, persisted assistant
acknowledgements, generic writer failure, run source isolation and
same-run cursor replay. The existing full Web feature route and UI
are unchanged when the switch is unset.

**M2.1 remains PARTIAL:** this explicitly limited chat path omits
rich uploads, user preferences/skills/RAG/memory, research, fallback,
full tool/approval cards, editor integration, comprehensive model
metrics, custom output limits and complete Web parity. These
in-process tests do **not** prove real-provider correctness, production
readiness, live multi-client behavior or a safe default cutover.
Web rollout, real-model acceptance and the final feature matrix
remain required exit gates. Do not enable the environment flag on
the private preview without separate authorization.

### First opt-in live Web HTTP → Gateway composer slice — November 5, 2026

The existing authenticated `POST /api/chat_stream` route now has a **disabled-by-
default** text-only branch wired directly to the original admitted Gateway
composer, inherited Agent Loop, shared context ports, and admitted legacy
user/assistant/partial writers. This is the first actual route-internal
cutover, not another mocked preview. It does not add an HTTP endpoint,
parallel Agent engine, persistent event store or new authorization grant.

**Selection is deliberately narrow and two-party opt-in:** the server must
set `MNERYS_M21_WEB_TEXT_ROUTING=1`, and the request must explicitly send
`gateway_profile=text_v1`. The client must send a plain `chat` message,
`no_memory=true`, `use_rag=false` and `active_doc_state=none`. Only the
strictly enumerated form fields are accepted. Agent/tools, approval controls,
research, selected notes, attachments, Web search, Plan/Compare, incognito,
active documents/email, workspaces, model switching, thinking overrides,
fallback, image generation, internal tool requests and unsuitable account
privileges all stay on the **existing Web path**. The operator switch is
disabled in every default installation; the Web UI does **not** automatically
send the new profile. No configured setting or user data is migrated.

The [Web-only adapter](../../routes/gateway_text_adapter.py) binds the
original [LegacyChatContextPorts](../../routes/chat_helpers.py),
the existing assistant and stopped-response writers, and a trusted
`chat` interaction marker to the same one-call composer. The original
`agent_runs` detached registry marks these explicitly as Web text runs
before exposure, so the existing `resume` endpoint may replay only an
admitted Web-origin typed run, with an exact run ID and validated cursor.
SSE frames include the same browser-consumable `turn_mode`, `model_info`,
`delta`, confirmed `message_saved`, `final_response` and `[DONE]` sequence.
The original stop endpoint retains exact-run cancellation; an older run ID
cannot stop a replacement. A failed writer produces a sanitized error
without success or save acknowledgement. No extra DB writer was created.

[Route selection and actual HTTP acceptance](../../tests/test_mnerys_gateway_web_text_cutover.py)
cover the server/client opt-in, original route selection and legacy fallback,
real `StreamingResponse` delivery via ASGI, original synthetic Agent Loop,
confirmed message persistence, a unique completion, upstream/writer failure,
owner-bound same-run replay, cross-adapter replay denial, and stale/current
Stop. The [existing writer AST regression](../../tests/test_mnerys_gateway_assistant_persistence.py)
now checks the two inherited cancellation-save calls **plus** the single
trusted Gateway writer binding without weakening their authorization checks.

**Exit status still PARTIAL:** this explicitly selected small profile is
not full Web Chat parity. It intentionally excludes the Web memory/skills
context, auto-naming/post-response tasks, full model/provider options,
richer chat formatting, browsing, attachments and tool/approval workflows.
A controlled **real provider** turn, broader Web-versus-direct acceptance
and full CI remain required before changing the default route. The
in-process run buffer is not durable after restart, and M2.2/M2.3 storage
and fencing work are separate. No private preview was deployed.
