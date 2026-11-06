# M2.1 exit gate — one reusable admitted Agent turn

**Status: NEARLY COMPLETE — the composed Web cutover, typed display events, real-provider acceptance and HTTP parity tests are implemented behind a disabled-by-default operator switch; the remaining items are the operator audit pass and roadmap flip once the hosted CI for the current batch is green.**

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

### HTTP strict-text post-response dispatch after confirmed completion — November 5, 2026

The disabled-by-default, server-and-client-opted-in Web text route now
reuses the existing [admitted post-response dispatcher](../../src/gateway_post_response.py)
and original [Web task runner](../../routes/chat_helpers.py), rather than
silently omitting notification, accounting and auto-naming behavior.

The trusted [HTTP binder](../../routes/gateway_text_adapter.py) provides two
optional callables to the existing
[text persistence contract](../../src/gateway_text_persistence.py):
`after_user_saved` fires the original Web `chat.message`/event-bus handoff
only after the inherited user append has produced a fresh receipt.
`after_assistant_saved` invokes `GatewayTurns.dispatch_post_response`
with the admitted owner/session, the same saved response, sanitized
usage, trusted user preferences and existing Web post-response runner,
only after the original assistant writer confirms its fresh receipt.
The source-owned Agent stream is closed before dispatch, so background
task scheduling cannot begin before the main model releases its turn
resources. These callables are selected by the Web server, not accepted
as request fields or tool permissions.

The original run manager still owns model execution, stop and replay.
The hook runs inside the **one original detached producer**, not the SSE
subscriber: a resumed client cannot save or schedule the response a
second time. Writer failure, user receipt failure and interrupted
partial replies do not dispatch a completed-response job. A failing
notification or post-response task after a confirmed write is logged
without its exception body; the message remains committed and the
client gets the proper confirmed completion rather than a false
uncommitted response or an automatic retry. A fresh Gateway owner
check is performed at the post-response boundary, even if the loaded
session changes during its legacy writer call.

[HTTP post-response acceptance](../../tests/test_mnerys_gateway_web_post_response.py)
includes the original synthetic model, real inherited writer receipt
checks, exact-run replay, early Stop, invalid callbacks, failed writes,
notification/task exceptions, owner changes and explicit ordering
`user_saved → model → assistant_saved → model_closed → post_response`.
No production database or provider is exercised, and the server switch
stays **off by default**.

**Remaining limitations:** this is post-response *handoff parity for
the strict no-tools/no-memory profile*, not full Web job equivalence.
Background extraction is deliberately blocked for this restricted
profile; full memory, preset, skills, uploads, tool results, auto-
escalation, real-provider parity and all post-response metadata still
belong to the broader M2.1 acceptance gate. No durable task delivery,
cross-process idempotency, database migration or preview deployment
is part of this change.

### Owner-consented foreground fallback in the real strict-text Web route — November 5, 2026

The disabled-by-default /api/chat_stream cutover now accepts a
server-selected **foreground model fallback** within its existing strict
plain-text/no-tools profile. This closes one previously inherited-only
Web/Core model-routing gap without allowing caller-defined provider URLs,
credentials or model lists. The existing request still needs an operator's
MNERYS_M21_WEB_TEXT_ROUTING=1 switch and an explicit client
gateway_profile=text_v1 with no memory, RAG, rich editor or tool features.

After the authenticated Web route resolves the original
[admitted foreground policy](../../src/gateway_foreground_routes.py)
and account model allowlist, it passes only a bounded set of
owner-allowed fallback **model names** to the trusted
[HTTP binder](../../routes/gateway_text_adapter.py). The
[Core composer](../../src/gateway_turn_composer.py) then **re-resolves
the current owner's preference** and the actual endpoint/headers,
intersecting its candidates with that trusted name set. A revoked or
ineligible fallback remains disabled. The model selector and retry
eligibility are unchanged in the existing Agent Loop; the new Web
adapter neither schedules second effects nor invents retry rules.
No toggle, fallback endpoint or authorization header is accepted from
Web request data.

An accepted source-owned native `model.route.selected` signal becomes
the existing Web `fallback` SSE notification, with requested model,
answering model, candidate index and round only. The original
[text persistence wrapper](../../src/gateway_text_persistence.py)
validates that the Core's selected model matches the admitted session
model and that the answer model has a bounded, safe identifier, then
passes exactly `requested_model` and `model` into the **existing**
assistant saver metadata. This avoids silently attributing a successful
backup answer to the originally selected model. The same metadata
reaches the original post-response token accounting/notification handoff.
Invalid or forged route attribution is rejected before an assistant
commit. Provider-authored fallback frames cannot create native Core
observations.

[HTTP and core acceptance](../../tests/test_mnerys_gateway_web_fallback.py)
covers owner-approved 503→backup recovery through the real inherited
Agent Loop, allowed-model filtering, explicit HTTP route selection,
source-owned Web event rendering, saved answering-model attribution,
exact-run replay with a single assistant receipt/one post-response
dispatch, owner revocation, non-eligible failure, already-visible
primary output, malformed model names and unauthorized route metadata.
Providers, user preferences, embeddings and databases are fixtures.

**Still PARTIAL:** this is restricted to the experimental text-only
Web/Gateway path, not universal Web model fallback parity. Full rich
attachment/context metadata, mixed tools, permissions/approval UI,
all provider modes, real provider acceptance, after-restart replay
and default Web cutover are not claimed. The saved answer's two model
IDs are legacy message metadata, **not** a durable per-candidate event
journal, endpoint audit receipt or exactly-once transaction.

### Verified small text attachments in the opted-in real Web turn — November 5, 2026

The existing, **off-by-default** /api/chat_stream strict Web/Gateway
profile now supports previously uploaded, UTF-8 **text** files through
the original upload index, owner-bound manifest and inherited
ChatHandler preprocessing. The browser sends only attachment IDs, never
paths or credential-bearing file content. The adapter accepts at most
four distinct IDs and each file is bounded to 64 KiB; only the already
approved plain-text/Markdown/CSV/JSON/log extensions and text MIME
contracts are eligible. A file type not covered by that contract is
rejected by the same Core validator, not interpreted as tool authority.

The [strict HTTP selector](../../src/gateway_web_text_cutover.py)
requires an exact JSON array of string IDs in either the existing Web
form attachment field or the JSON request body (never both). It
rejects integer-to-string coercions, duplicate selectors, invalid
IDs, ambiguous/repeated form fields and oversized selections. Invalid
profiles remain on the existing Web path. No new upload endpoint,
filesystem grant or tool scope was created.

The [shared composer](../../src/gateway_turn_composer.py) now accepts a
**separately server-selected** persisted-text-attachment mode,
restricted to an admitted `chat` profile without any tool, workspace,
active document or approval continuation. It resolves uploads through
the [owner-checked file snapshot](../../src/gateway_attachment_context.py)
before preprocessing, checks canonical upload-root confinement,
file identity, size, MIME, UTF-8 and checksum, and **rechecks** the
unchanged snapshot after asynchronous preprocessing. Unlike the
earlier direct no-persistence profile, it defers the inherited user
writer until *after* both checks. The original user writer receives
the human question and original attachment references, but **not**
the extracted attachment body as a future trusted user message.
The text body goes to the current model only via a separately marked
`untrusted_context_message` followed by the original user request.
Tools remain blocked; uploads do not grant read_file or shell access.

[HTTP/owner/upload acceptance](../../tests/test_mnerys_gateway_web_text_attachments.py)
covers real ASGI SSE delivery, original ChatHandler preprocessing,
the inherited user and assistant writer receipts, one-time
post-response dispatch, exact-run replay, malformed form/JSON
selectors, foreign owners, file replacement during processing,
oversized/binary/unsupported/symlink inputs and denial before
message/model effects. The earlier non-persisting direct attachment
profile stays unchanged; attaching files to arbitrary direct
persisted turns remains denied.

**Exit status still PARTIAL:** this is a narrow read-only text-upload
profile with a required textual question. It does not support
image/audio/PDF extraction, attachment-only requests, future-turn
recovery of extracted text, general Web attachment/source UI
parity, transactional multi-message writes, native remote file
resources, real-model acceptance, or a default Web cutover. The
in-process run and upload index are not durable Gateway resources.

## Restricted native notes persistence — November 5, 2026

A server-created persist_verified_native_tools option extends the
[direct turn composer](../../src/gateway_turn_composer.py) and the
[original text persistence wrapper](../../src/gateway_text_persistence.py)
to one native manage_notes surface in **Agent** mode. This is a
process-local integration profile, not a client-accepted flag. The existing
owner admission, account/tool policy, immutable tool contract, dispatcher,
user append, assistant writer, and fresh database message receipt all
remain mandatory.

The profile denies every other offered tool, client/tool-controlled
workspace, active document, file upload, unsupported approval continuation,
and missing persistence writer. Sealed notes-only continuations are separately
admitted as described below. Normal successful tool turns emit filtered native
status, the original confirmed message receipt, final answer and completion.
When the original Loop requests a sealed approval, the turn instead ends
in turn.awaiting_approval after the approval/tool observation, without
persisting an assistant response or manufacturing a cancelled partial.
Sequence numbers remain contiguous after buffering the unsaved final text.

[Native persistence regression tests](../../tests/test_mnerys_gateway_native_tool_persistence.py)
exercise the actual inherited Agent Loop's immutable notes-read operation
and mocked tool/provider turns, completion ordering, approval pause, no
duplicate writes, and fail-closed configuration. They use synthetic
effects and an in-memory writer; they do **not** prove a live model or
production DB transaction.

**Exit status remains PARTIAL.** Default Web tool routing remains legacy.
A restricted HTTP notes route is now available behind a disabled-by-default
server opt-in, but the Web UI does not select it automatically. Transactional
tool-side-effect idempotence, durable receipts, restart replay and
full Web/CLI/Telegram parity remain open. The narrowly scoped, process-local
persisted continuation is described below. Never treat a persisted user
message as proof that an earlier tool effect can be repeated safely.


## Persisted native notes approval round-trip — November 5, 2026

The existing **server-selected, in-process** `manage_notes` persistence
profile now supports the original sealed approval continuation. It runs
through the **same Gateway composer, inherited Agent Loop, tool dispatcher,
ToolApprovalStore and assistant writer** as the initial request. The trusted
profile still permits only `manage_notes` in Agent mode without uploads,
workspaces or active documents. Only the trusted adapter can select this
profile; HTTP inputs never select tool authority directly.

A pending review pauses after **one confirmed user message**, without
fabricating an assistant save. Before consuming an approval or denial,
the composer requires the most recent legacy session message to have the
original exact request text, user role, a nonempty, unique post-commit
`_db_id`, and a matching sealed review. An approval also repeats this
check **after asynchronous context preprocessing** to reject a newer
session write or altered receipt before the one-use decision is consumed.
Neither approval/denial labels nor provider text grant tool authority.
The original account policy, immutable contract and sealed exact action
are revalidated on continuation.

A denied action never executes the model or tool and writes its visible
denial response through the existing admitted assistant writer. An approved
action proceeds through the original Loop and dispatcher **at most once**,
without writing the original user message again. A final reply is emitted
as completed only after the legacy assistant save has a fresh receipt.
If a writer fails after an effect, the stream reports a generic persistence
error and **does not retry** the consumed approval or tool action.

[Persisted review regression tests](../../tests/test_mnerys_gateway_persisted_native_review.py)
cover request/pause/approval/denial, actual original Loop and dispatcher,
one-use effect, missing/duplicate/changed receipts, newer messages during
context preprocessing, policy revocation, saved response ordering, writer
failure and replay denial.
[Disposable SQL writer acceptance](../../tests/test_mnerys_gateway_text_sqlite_integration.py)
verifies the same paused/resumed lifecycle against the original SQLite and
PostgreSQL 17 writers and confirms one committed user message and one
assistant response. The existing tool-approval and required-read regressions
were also rerun. Local focused validation passed **127**, skipped **8**
PostgreSQL-only parametrizations when the disposable DB was absent; a
separate temporary PostgreSQL 17 container passed **61** cross-backend
tests and was removed afterward. These suites overlap.

**Exit status remains PARTIAL:** the origin check is a **process-local,
legacy receipt check**, not a durable association between a pending tool
effect and an immutable transaction. It cannot protect against an
unfenced DB owner transfer racing a write, guarantee idempotence after
process restart, or recover a consumed effect if its reply save fails.
Default Web tool routing, complete interactive UI parity, Telegram/CLI
clients, multimodal requests, durable event/approval journals,
real-model acceptance and transactional effect recovery remain open.
No production database, provider, preview deployment or default Web
configuration was changed.


### Persisted native-notes Web SSE and future HTTP input selection — November 5, 2026

The [existing Gateway facade](../../src/gateway_turns.py) now offers
`preview_persisted_native_notes_web_sse` as a **server-only, process-local,
non-detached preview**. It reuses the original admitted composer and
legacy writers for one `manage_notes` tool turn and converts the filtered
native events to browser-understood SSE using the existing
[Web compatibility serializer](../../src/gateway_web_compat_events.py).
The status timeline exposes only vetted tool names, rounds and outcomes,
never tool input/output. A pending review card is read twice from the
**original sealed ToolApprovalStore**, under the same admitted owner,
session, action and digest, before it becomes a browser `ask_user`.
The review remains unconsumed until the separately admitted continuation.
Approved and denied continuations reuse the persisted original user
receipt; a completed answer requires the **original assistant-save receipt**
before `message_saved`, `final_response` and `[DONE]`. A failed
writer after a tool effect emits only a sanitized error without a false save
acknowledgement or automatic replay.

The [HTTP input selector](../../src/gateway_web_notes_profile.py) is
a **pure parser**, not a route or permission. It is now used only by the
restricted, server-gated Web branch; it requires an exact `notes_v1`
profile with a server-matched session,
Agent mode, no memory/RAG or editor context, and a single bounded question
or an opaque approval ID with a valid decision. It rejects repeated form
fields, mixed form/JSON values, unexpected feature toggles, supplied
endpoints, tool grants and typed JSON coercion. It never reads or consumes
the sealed store and cannot mint a principal, authority or policy.
The production `/api/chat_stream` route calls this selector only when the
client explicitly requests `notes_v1` and the dedicated server switch
is enabled; malformed `notes_v1` inputs are rejected, not passed to legacy.

[Persisted Web notes SSE acceptance](../../tests/test_mnerys_gateway_persisted_web_notes_preview.py)
uses the real composer, in-memory original writer, sealed pending records
and a **test-only** FastAPI/ASGI endpoint to verify complete approval cards,
one-use approval and denial, tool-output suppression, write acknowledgements,
writer failure and forged/unsupported contexts.
[Profile selection negatives](../../tests/test_mnerys_gateway_web_notes_profile.py)
pin the exact permitted request fields, server opt-in, session equality
and rejection of ambiguous/unsupported controls. Combined focused
SSE, profile, native-tool and prior approval suites passed **108 tests**.

**Not a general production cutover:** the initial, direct preview
is **non-detached**; the separately tested detached implementation is used
only by the dedicated, disabled-by-default HTTP branch described below.
It reuses the existing interactive-human approval requirement, account/model
admission, current policy and one-use decisions without consuming a
review in the legacy route. The default Web route behavior remains unchanged.


### Detached persisted notes SSE and exact-run review replay — November 5, 2026

The restricted `manage_notes` Web-format candidate now supports a
**process-local detached lifecycle** through the *existing*
[agent_runs](../../src/agent_runs.py),
[TurnService](../../src/turn_service.py), and
[GatewayTurns](../../src/gateway_turns.py). It is not an alternate
Agent Loop, client transport, approval database or replay journal.
`web_native_notes` is a mutually exclusive, server-issued typed-run
provenance marker; `text_v1`, internal typed runs and inherited SSE cannot
resume as native-notes Web runs or inherit their Stop authorization.

`start_detached_persisted_notes_web_sse` admits an owner-scoped, persisted
single-tool request, and preflights a sealed review plus its existing
committed user-message receipt before a continuation can replace a run.
Delegated API-token principals cannot approve; those decisions require
interactive human authorization. An active run is never replaced by this
preview while still executing. Each issued run carries its own opaque
ID, one in-memory typed-event buffer, and an exact sequence cursor.
Disconnecting an SSE subscriber leaves the original execution running;
reconnecting through
`resume_detached_persisted_notes_web_sse` does **not** call the model,
re-execute a tool, or write a message.

Pending-review replay deliberately processes the **full original typed
prefix** to reconstruct and revalidate the same sealed pending card, then
suppresses frames at or before a validated `Last-Event-ID` cursor.
The `ask_user` card carries the original `turn.awaiting_approval`
cursor, but the terminal `[DONE]` is never an acknowledgment of an
executed action. Reconnecting after that cursor does not redisplay the
card. If the stored review was consumed, retired, tampered or expired,
the original replay **fails closed**; a dead card is never resurrected.
The resumed authorized action runs as a **new, separate** detached
turn under its one-use exact-action proof; the previous run becomes
non-replayable after replacement.

An exact-provenance Stop method refuses stale/foreign run IDs.
Provider-idle heartbeats are permitted only as narrowly formatted,
actionless SSE comments: they receive no event ID, receipt or authority.
The inherited writer still gates `message.saved` and completion;
a failed writer after an effect emits only a generic error and is
never automatically retried.

[Detached notes replay tests](../../tests/test_mnerys_gateway_detached_persisted_notes_web.py)
cover SSE disconnect/reconnect, cursor suppression, unchanged run identity,
completed turn replay, pending-review cursor, one approved effect, denial
of cross-provenance access, delegated-token approval, unauthorized owner,
stale/invalid cursor, Stop and active-run replacement veto, stale-review
replay, sanitized writer failure and heartbeat filtering. A test-only
FastAPI/ASGI endpoint also verifies the detached run ID and HTTP SSE cursor
reconnection without a repeated effect. The local suite passed **10 tests**;
the older direct preview and HTTP selector tests remain separate. All tests
use synthetic models, in-memory sessions and the original Gateway/Loop —
no production remote endpoint is enabled.

**Still open:** the general authenticated Web route, full existing
browser UI parity, durable post-restart replay, a transactional effect
ledger, multi-worker fencing, event journaling and real-provider acceptance
are not complete. The restricted HTTP branch below is not enabled by
default. The M2.1 exit gate remains **PARTIAL**.

### Guarded notes_v1 HTTP route admission — November 5, 2026

The original [Web chat routes](../../routes/chat_routes.py) now contain a
strict, **disabled-by-default** `notes_v1` branch controlled by the
**server-only** `MNERYS_M21_WEB_NATIVE_NOTES_ROUTING=1` setting.
This is a narrow, real HTTP integration using the
[notes adapter](../../routes/gateway_notes_adapter.py), not a replacement
for the inherited Web chat handler. The current browser does not
automatically choose this profile; a client must explicitly opt in,
and the server switch is off unless an operator enables it.

The opt-in is intercepted **before** the legacy Web code can consume an
approval or retire a pending review. Unknown, malformed, duplicated,
mixed-source or unsupported `notes_v1` requests return HTTP 400/403
rather than falling back to a broader legacy tool route. The branch
uses the existing chat-scope requirement, admitted session ownership,
server-owned route reconciliation, post-reconciliation allowed-model and
message-quota checks, session credential resolution, and both
owner privileges and server-global disabled tools. It rejects internal
tool requests, unsupported workspaces, uploads, RAG/memory, Web search,
other tool families, client model/endpoint overrides and paid model
fallbacks. `manage_notes` remains the sole offered tool, subject to
the **original Core tool policy, inventory, request authority and
dispatcher checks** — the profile name never grants permission.

For approval/denial, the route first applies the inherited
interactive-human requirement and refuses delegated bearer credentials.
The server-issued pending review, original committed user receipt and
one-use exact action are then revalidated by the shared Gateway composer.
The legacy approval consumer is **not called** on `notes_v1` requests,
so it cannot consume the same approval a second time. A continued action
has its own detached run and does not save a second user message.
The dedicated SSE envelope returns the original
`X-Odysseus-Run-Id`; `GET /api/chat/resume/{session_id}` uses this
exact run ID and an optional `Last-Event-ID` to replay only the remaining
typed events, including during the bounded **completed** in-memory window.
`POST /api/chat/stop/{session_id}` stops only the owner-admitted,
matching native-notes run. A pending approval is checked against the
original sealed store before the resume response is created, as well as
before an actionable card is emitted.

The strict form or JSON initial request has the following fields:
`gateway_profile=notes_v1`, `session` (the owned session ID),
`mode=agent`, `no_memory=true`, `use_rag=false`,
`active_doc_state=none`, and a nonempty `message`.
A decision uses the same base fields with `tool_approval_id` and
`tool_approval_decision` (`approve`, `approve_task`, or `deny`)
instead of a new user message. Form booleans are the lowercase strings
`"true"` and `"false"`; JSON booleans must be actual booleans.
The client cannot supply a Principal, tool contract, approval store or
server-disabled-tool override.

[Production-route selection tests](../../tests/test_mnerys_gateway_web_native_notes_route.py)
verify explicit opt-in, authorization ordering, model rechecking,
invalid-input rejection, server-disable propagation, delegated-token
review denial and bypass prevention for legacy approval consumption.
[HTTP route lifecycle and ASGI tests](../../tests/test_mnerys_gateway_web_native_notes_integration.py)
exercise the actual `/api/chat_stream`, `/resume` and `/stop`
handlers with synthetic model and writer observations, one-use approval,
confirmed original user and assistant receipts, exact cursor replay,
stale-run behavior and global tool-disable rejection. These tests do
**not** use the live provider or the production database.

**Remaining M2.1 work:** the generic Web/CLI/TUI/Telegram clients still
require complete adapter parity and contract coverage. The Web frontend
does not automatically **start** a `notes_v1` conversation. It can now
restore an existing, admitted notes review while the original process-local
run remains available; recovery after buffer eviction or server restart
is not implemented. There is no durable approval journal, multi-worker/run
fencing, transactional tool-effect ledger, restart recovery or real-model
acceptance. The inherited post-response handoff has been added for notes,
but its full behavior on a real configured account remains unverified.
Keep the opt-in off for production use until browser/desktop/mobile,
provider and durability cases are accepted. No production instance,
database schema or default UI behavior was changed.


### Notes Web post-response, pending-card recovery and sealed origin — November 5, 2026

The [restricted Web notes adapter](../../routes/gateway_notes_adapter.py)
now dispatches the **same inherited Gateway post-response runner** as the
existing `text_v1` adapter, **once only after** the original assistant
writer's confirmed receipt and Agent Loop cleanup. It uses the original,
owner-admitted request text; a continuation restores that text from the
sealed pending review rather than the browser button. For this narrow
tool-enabled profile, memory/skill background extraction is disabled;
the inherited runner retains its token accounting, permitted notifications
and automatic chat naming. A failing optional runner never undoes or
retries the assistant write, and typed SSE replay never dispatches a job.

The [original Web session status route](../../routes/chat_routes.py) can
report a paused `notes_v1` review as `awaiting_approval` **only** for
an already-authenticated, interactive owner while
`MNERYS_M21_WEB_NATIVE_NOTES_ROUTING=1`. It identifies the original
completed typed run in the existing [agent_runs](../../src/agent_runs.py)
registry, revalidates its one still-pending sealed card and returns
no approval content through the status endpoint. An exact-run replay
reconstructs and rechecks the original card, without running a model
or tool or saving a second message. The
[existing browser stream](../../static/js/chat.js) recognizes the
server-origin `X-Mnerys-Gateway-Profile: notes_v1` response header
and renders the pending card using the **existing ask_user renderer**,
without a fabricated assistant bubble. Session re-entry uses the
[existing sessions module](../../static/js/sessions.js) to query the
read-only paused status and resume after navigation or refresh. A
review button sends only the strictly allowed notes form fields, and
the server still requires interactive human consent and the current
sealed exact-action authorization. The ordinary chat composer, old
approval cards and `text_v1` flow retain their inherited behavior.

**Cross-profile isolation:** a run-origin `notes_v1` marker is assigned
by a trusted context binding when the original detached Agent task is
created. The [original ToolApprovalStore](../../src/tool_approvals.py)
stores it in the **canonical digest-bound pending record**, not in an
untrusted client field or a separate registry. The old Web approval
consumer refuses a notes-origin record *before* calling its existing
destructive approval operation, **even if the run registry was evicted**.
Conversely, the restricted notes continuation refuses legacy-origin
approvals. A changed marker invalidates the exact-action digest.
Neither the HTTP profile flag nor the browser response header confers
authority to execute a tool.

[Post-response regression](../../tests/test_mnerys_gateway_web_notes_post_response.py)
verifies commit/Agent-cleanup/job ordering, one-time replay, failed
writer, failed optional runner and sanitized failures.
[Browser recovery route tests](../../tests/test_mnerys_gateway_web_notes_ui_recovery.py)
cover restore without a client run ID, exact cursor, delegation denial,
retired-card denial and no duplicate assistant persistence.
[HTTP tool approval lifecycle tests](../../tests/test_mnerys_gateway_web_native_notes_integration.py)
and [detached review tests](../../tests/test_mnerys_gateway_detached_persisted_notes_web.py)
cover the provenance marker, two-way cross-profile denial, digest binding
and prevention of legacy consumption after a run buffer is removed.
The [isolated Playwright Chromium browser contract](../../tests/e2e/gateway-notes/browser-ui.spec.js)
tests the actual `chat.js`, `chatRenderer.js` and `sessions.js` modules:
a paused notes SSE restores exactly one review card with no phantom assistant
bubble; the card renders safely on desktop and mobile without approving via
digit shortcuts; approval/denial use the strict eight-field form; a
`notes_v1`-tagged non-notes review cannot become a tool card. JavaScript
syntax is also checked with Node. The [test harness](../../tests/e2e/gateway-notes/serve.py)
binds only loopback, exposes public static files and injects synthetic
HTTP/SSE responses; it does **not** initialize the application's configured
MCPs, memory store, model providers, scheduler or database. This is a
**browser UI contract**, not a full authenticated app/provider acceptance
or a restart-recovery test. The normal Web UI still does not automatically
start a `notes_v1` conversation.

### Browser-to-Gateway HTTP acceptance with synthetic model — November 5, 2026

The [isolated Gateway browser integration](../../tests/e2e/gateway-notes/integrated-browser.spec.js)
runs Chromium against a [test-only ASGI fixture](../../tests/e2e/gateway-notes/gateway_fixture.py)
that mounts the **real** restricted `/api/chat_stream` and
`/api/chat/resume/{session_id}` route callbacks, the original Gateway
composer, sealed pending store, in-memory typed-run buffer, inherited
assistant writer and post-response hooks. It explicitly does **not** start
`app:app`, perform real account authentication, contact a model or
external MCP, or connect to the production database. Its fake principal,
session and response streams are confined to the fixture process.

Two browser-to-route tests verify that a real browser can recover the
pending `ask_user`, click its approval option, submit the exactly admitted
`notes_v1` form and receive the writer-confirmed SSE answer. A repeated
claim is rejected; the original user message, effect, assistant response
and post-response hook are each recorded **once**. The second test uses
the actual production HTML markup with the automatic bootstrap scripts
removed, and calls the original exported `handleChatSubmit` while
the normal composer button is disabled. That exposed and fixed a UI
coupling in [chat.js](../../static/js/chat.js): notes approval decisions
now invoke the existing single-flight submission handler directly,
independent of the composer button's visual enabled state. Legacy cards
retain their existing button behavior.

Run via `npm run test:gateway-notes-http` (override
`MNERYS_NOTES_E2E_PYTHON` when using a non-default virtual environment).
The fixture server binds only loopback, refuses startup without its
test-only flag, and receives no application provider secrets. Playwright
configuration and output follow the
[browser test documentation](../../tests/e2e/README.md).
This closes the **synthetic browser-to-HTTP** integration gap, not the
real-user/real-provider acceptance gate. Full authenticated application
startup, actual provider invocation, persistence across restart and
multi-worker behavior remain unverified.

**Durability limitation:** the typed run's bounded replay window is
currently **180 seconds**, whereas the shared pending approval TTL is
**600 seconds**. After the run buffer disappears, its notes-origin
approval still **cannot be consumed by the legacy adapter**, but the
browser cannot reconstruct the card through the process-local replay.
Neither an interrupted server process nor a worker switch can restore
the action. A metadata-only transactional decision fence is now staged as
an explicitly migrated component (see below), but durable serialized
review/approval recovery and event replay remain open M2.1 work.
The HTTP switch stays off by default.


### SQL notes decision fence — November 5, 2026

The original persisted notes path now has an **explicitly installed,
metadata-only SQL decision ledger** in
[the fence module](../../src/gateway_notes_decision_fence.py), separate
from the inherited in-memory ToolApprovalStore and Agent run buffer.
Both SQLite and PostgreSQL 17 implement a partial unique index limiting
pending reviews to one per owner/session and conditional transactional
updates limiting each sealed approval to one decision.

For a notes_v1 HTTP request, the [Web adapter](../../routes/gateway_notes_adapter.py)
checks the reviewed schema before the original user can be persisted.
An approval can become actionable only after the original, owner-admitted
pending store and confirmed user-message receipt have been revalidated,
then the pending review has been **registered in SQL**. A separate server
callback executes **after all fallible policy/context preparation and before**
the original ToolApprovalStore consumes its one-use approval. It reserves
the exact owner, session, approval ID, sealed digest and original user
receipt transactionally. Invalid, expired, foreign, superseded or
previously reserved decisions fail closed. The inherited writer's
confirmed assistant receipt is recorded afterward; a failed callback
cannot cause automatic re-execution. Denied approvals are terminal
without a tool effect.

The guarded status/replay handlers also check the SQL registration before
exposing a pending card. The production route does not auto-create the
table: an absent or incompatible migration returns a sanitized HTTP 503
before any new user message or tool action. Installation is a separate,
authorized operation, **not** part of app startup or this test run.

[Decision fence tests](../../tests/test_mnerys_gateway_notes_decision_fence.py),
[migration tests](../../tests/test_mnerys_gateway_notes_fence_migration.py)
and [real route integration tests](../../tests/test_mnerys_gateway_notes_decision_http.py)
exercise isolated SQLite and disposable PostgreSQL 17, cross-owner and
digest checks, two-connection atomic one-use claims, expiry, supersession,
migration failure, and registration-before-card/reservation-before-effect
ordering. The [operator deployment and rollback plan](../deployment/notes-decision-fence.md)
records backup/restore prerequisites and a deliberately manual migration.

**Read-only restart classification:** on re-entry with no original process-local
run, an interactive owner can query a durable notes-only status keyed by the
original SQL user receipt. The Gateway returns `review_unavailable` with
`interrupted`, `expired`, or `indeterminate`, never an approval ID or
tool/content payload. The [session UI](../../static/js/sessions.js)
shows one non-actionable, themed status notice with no approve/deny controls.
The original [read-only SQL query](../../src/gateway_notes_decision_fence.py)
prioritizes reserved/uncertain actions over a pending review from the same
user receipt. Later messages and delegated credentials do not regain old
review state. [Isolated route tests](../../tests/test_mnerys_gateway_notes_read_only_recovery.py)
and [Chromium HTTP integration](../../tests/e2e/gateway-notes/integrated-browser.spec.js)
simulate run eviction while preserving the original SQL ledger, confirming
the safe notice without any new action.

**Not yet durable recovery:** SQL stores metadata only, not the original
signed/capability-bound pending approval or the typed event transcript.
A process crash after reservation may leave an **uncertain** tool effect;
that row cannot be reserved again, but no automatic resolution is
available. A fresh process also cannot reconstruct an actionable card
from the SQL row. Automated retention/deletion policy, multi-worker
session leadership, **raw SSE event** durability, reconciliation and
real-provider acceptance remain open. The notes_v1 feature flag stays off by default.

### Restricted durable notes event journal — November 5, 2026

The [metadata-only event journal](../../src/gateway_notes_event_journal.py)
records only four server-origin lifecycle milestones: `review_registered`,
`decision_reserved`, `decision_denied`, and `assistant_committed`.
No model-generated text, private note, tool argument/output, credential,
untrusted JSON or signed approval payload is stored. The restricted
[notes HTTP adapter](../../routes/gateway_notes_adapter.py) writes the
review marker after its already-verified one-use SQL fence and before the
browser can see an actionable card. A reservation is journaled after SQL
has irreversibly fenced the decision and before the original Core tool
consumption. Confirmed assistant commits are journaled after the original
writer receipt; journal failure there never re-executes a tool.

The owner-admitted `GET /api/chat/notes_journal/{session_id}` endpoint
provides **non-actionable** milestones for the current committed user
receipt (or its immediately following confirmed assistant response),
with `after_cursor` and `limit` (1–100). Only an opaque
scope-bound cursor, event kind and server timestamp are returned. The
internal database sequence, approval ID and all other private payloads
are excluded. Delegated credentials, other owners,
other sessions and stale user receipts cannot read another timeline.
These database cursors are **not** SSE run cursors and cannot revive a
signed approval, model response or tool effect.

The journal schema is independent of the decision-fence table and is
**never auto-installed**. The [explicit migration](../../scripts/migrations/notes_event_journal_v1.py)
requires prior fence installation, an approved backup/restore and operator
authorization. It is read-only by default. The journal uses a monotonic internal database sequence, but exposes
only random 32-character opaque pagination tokens to avoid leaking
cross-session event volume; SQLite uses AUTOINCREMENT so its internal
sequence cannot recycle IDs after retention. A reviewed manual `prune_terminal` operation
requires at least 30 days of retention, processes bounded batches, and
deletes only events whose separate one-use fence is completed, denied or
superseded. Reserved/uncertain and pending review events are preserved;
the original fence table is never pruned by this method.

[Storage and retention tests](../../tests/test_mnerys_gateway_notes_event_journal.py),
[migration tests](../../tests/test_mnerys_gateway_notes_event_journal_migration.py),
[HTTP lifecycle tests](../../tests/test_mnerys_gateway_notes_event_journal_http.py),
and [synthetic Chromium integration](../../tests/e2e/gateway-notes/integrated-browser.spec.js)
cover scoped replay, failure before an actionable card, reservation
journaling failure, cross-worker uniqueness, SQLite and disposable
PostgreSQL 17 migration installation/fresh-engine replay, terminal
retention and blocked cursor reuse. The complete
[operator plan](../deployment/notes-event-journal.md) documents migration,
manual retention, rollback and limits. On `review_unavailable`, the
[Web session UI](../../static/js/sessions.js) now retains the original
non-actionable warning and optionally displays up to four recent,
allowlisted journal milestones as plain text. Browser integration tests
cover expected readback, tampered event kinds/HTML and journal outages.
It does not render a full SSE timeline, recover an approval card or
replay a tool effect.

### Independent process-restart regression — November 6, 2026

The [fresh-interpreter restart test](../../tests/test_mnerys_gateway_notes_restart_process.py)
creates a verified, disposable SQL review, then separately invokes brand-new
Python interpreters to read the original record, reserve a one-use decision,
observe the subsequent indeterminate state and reject a repeated decision.
The test exercises file-backed SQLite and isolated disposable PostgreSQL 17,
checks that delegated credentials cannot read the journal and rejects any
unapproved or non-disposable test backend. No Agent Loop or tool dispatcher is
restarted, and no signed approval is reconstituted. This verifies durable
**metadata-only** recovery across process boundaries, not durable SSE delivery,
resumable tool execution, multi-worker coordination or production restart.

### Hosted source-contract regression follow-up — November 6, 2026

The first hosted CI run for commit `6d285d1c` passed shards 1, 2 and 3,
Python/JS syntax and the CodeQL analyses, but shard 4 found **two stale
source-shape assertions**: the original `ask_user` renderer is now invoked
through the notes-aware wrapper, and the notes route adds a fourth trusted
interrupted-writer binding. The
[card persistence contract](../../tests/test_ask_user_persistence.py) now
requires both the live/replayed wrapper and unchanged legacy renderer; the
[admitted persistence contract](../../tests/test_mnerys_gateway_assistant_persistence.py)
now checks the exact notes call and original inherited writer. **93 focused
tests passed locally** including required-read isolation. This is a test
contract repair, not an expansion of tool permissions or restart recovery.
A new hosted all-shard CI run is still required for acceptance.

**Still not implemented:** durable raw SSE replay, signed approval-card
restoration after process failure, multi-worker session leadership,
transactional whole-turn persistence, approved retention automation,
real-provider acceptance and the full four-shard CI gate. The feature
switch stays disabled by default.

## Full Web feature set through the single composition — November 6, 2026

The [direct turn composer](../../src/gateway_turn_composer.py) and the
Gateway facade now compose the production Web turn's full feature set
through one admitted operation, and the actual `/api/chat_stream` route
can run through it.

**Composer coverage.** `TrustedTextTurnSettings` now carries
server-selected context features (memory, RAG, web, incognito, inherited
ChatHandler preprocessing), the owner's visible email review context
(uid/folder/account plus bounded cached subject/preview; compose tools
denied while it is open), a text-and-image attachment profile
(owner-checked uploads, per-file identity/hash rechecks, chat-upload size
limit, non-persisting vision turns with multimodal user content passed to
the model), Web request options (preset, thinking mode, reasoning effort,
sampling temperature, tool budget, plan mode with approved-plan pin,
compare mode, pre-fetched search context), and a generalized persisted
native-tool surface (any offered native tool set, including
workspace-bound filesystem and process tools, instead of only
`manage_notes`). Incognito composed turns are non-persisting and cannot
resume approvals. Persisted tool turns still pause without an assistant
save on a pending review, and continuations still require the original
confirmed user receipt.

**Typed display events.** The [typed adapter](../../src/gateway_turn_events.py)
now emits bounded display-only families for hidden reasoning
(`thinking.delta`, never answer content or a partial save), agent round
progress (`agent.step`), turn-control annotations (`turn.control` with
only the bounded browser-consumed fields), and web search sources
(`sources.web`, filtered to public URL/title/snippet metadata). A trusted
client adapter may request `client_rendering`, which passes the browser's
own frame families (tool timeline with commands/outputs, `ask_user` cards,
plan/doc/editor/ui events, rich metrics and audits) through as validated
`client.frame` events with parsed payloads; the default metadata mode
keeps hiding those payloads. The [Web serializer](../../src/gateway_web_compat_events.py)
maps every family back to the browser's existing SSE vocabulary and fails
closed on unapproved shapes.

**Single Web composition.** [GatewayTurns.compose_web_turn](../../src/gateway_turns.py)
now runs the inherited Web chain in its exact order from one
transport-neutral operation over a `WebTurnComposition`: early intents,
post-session routing, context ports, compact runtime, document promotion,
contract scope, capability classification, delegated limits, web intent
reconciliation, incognito/email tool strips, final policy veto, runtime
UI projection, request authority, inventory/selection and the routed
contract. The one-use approval decision is consumed inside the sealed
store; a denial short-circuits before context I/O. Authority, contract
and policy objects are only ever derived server-side.

**Route cutover.** `POST /api/chat_stream` routes ordinary chat/agent
turns through `compose_web_turn` when the operator sets
`MNERYS_M21_WEB_CORE_CUTOVER=1`. The legacy inline chain remains
byte-for-byte as the fallback and for research, image-generation and
experimental profile turns. The route keeps parsing, session/endpoint
recovery, active-document resolution, serialization and the inherited
execution/saves block; no logic is duplicated and the flag is off in
every default installation.

**Real-provider acceptance.** A controlled real turn ran locally through
the composer against the private preview's authorized NVIDIA NIM
endpoint: the stored key was decrypted in-process (never echoed), the
model `minimaxai/minimax-m3.1-flash` answered "What is 2+2?" with "4"
and a terminal `[DONE]`. Small NIM models returned HTTP 404 (function
not enabled for the account), so the probe tries flash-class candidates
first. No credentials or private addresses were recorded in the
repository.

[Acceptance tests](../../tests/test_mnerys_gateway_context_features.py),
[image attachment tests](../../tests/test_mnerys_gateway_image_attachments.py),
[generalized tool persistence](../../tests/test_mnerys_gateway_general_tool_persistence.py),
[composition tests](../../tests/test_mnerys_gateway_web_compose.py),
[typed event tests](../../tests/test_mnerys_gateway_turn_events.py),
[Web serializer tests](../../tests/test_mnerys_gateway_web_compat_events.py)
and [route cutover tests](../../tests/test_mnerys_gateway_web_core_cutover.py)
cover the positive flows and the negative authorization, ownership,
identity, ordering and fail-closed cases. Local regression: 1,820 gateway
and chat tests passed (48 PostgreSQL-only skips). The hosted CI run for
the published batch is the remaining acceptance gate; the switch stays
off by default and no preview deployment happened.
