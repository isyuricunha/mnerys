# M2.1 typed direct-turn events — transitional adapter

**Status: PARTIAL — native terminal, completion-gated visible text, sanitized final metrics and generic failures reach the admitted, tools-disabled direct composer. Separately admitted options emit filtered native tool/approval metadata and source-owned fallback route choices; provider ingress and production Web still use inherited SSE.**

This is a bounded implementation record for the [M2.1 exit gate](M2_1_EXIT_GATE.md), not the final [durable Gateway event protocol](GATEWAY_AGENT_CONTRACT.md#event-schema--proposal).

## Supported flow

1. The trusted Python caller uses [GatewayTurns.open_composed_text_events](../../src/gateway_turns.py) with the **issued session admission**, server-loaded session, trusted context ports and server-selected [text-turn settings](../../src/gateway_turn_composer.py).
2. On first consumption, the composer validates owner/account permission and builds the shared prompt. Its default authority remains **tools-disabled**; an explicit server-owned native tool surface follows the inventory/selection/contract path described below. Both enter the inherited Agent Loop through [TurnService](../../src/turn_service.py).
3. The [Agent Core](../../src/agent_loop.py) can emit [CoreTerminalEvent](../../src/agent_core_events.py) directly on completion. Its [completion/evidence gate](../../src/agent_runtime/completion.py) now also produces native, post-filter **visible text, safe final usage counts and generic failure events**. The teacher decorator preserves these signals and prevents restricted direct turns from launching child executions. The [Gateway event adapter](../../src/gateway_turn_events.py) consumes the native objects directly and still interprets other inherited SSE frames; nested generators close before bridge/contract context variables are released.
4. Consumers can read events and outcome directly in Python, **without importing a Web route**. The production [Web adapter](../../routes/chat_routes.py) remains on its existing SSE transport, detached-run registry, approval and persistence paths.

This does **not** create another Agent Loop, an HTTP endpoint, a remote authorization mechanism or a persistence writer. A stream returned by `open_composed_text_events` starts lazily; a forged session is rejected when consumed, before context I/O.

## In-process data contracts

[TurnEvent](../../src/gateway_turn_events.py) is a frozen typed envelope: `sequence` (1-based, local to this stream), `type` (a permitted event name) and filtered `payload`. It does not contain a server-issued `turn_id`, a durable revision or a replay cursor.

[TurnOutcome](../../src/gateway_turn_events.py) is an evolving **in-memory** result snapshot with `state` (`pending`, `running`, `awaiting_approval`, `completed`, `failed` or `cancelled`), user-visible `content` on completion, last local `sequence` and a generic `failure_code`. In the default profile it is not an acknowledgement of database persistence; with trusted text persistence, completion is emitted only after the legacy assistant saver succeeds. Neither mode is a cross-process transaction.

[TypedTurnDelivery](../../src/gateway_turn_events.py) couples the async event iterator and its result snapshot.

## Native tool and approval metadata

An already admitted Python caller may opt into `native_tool_events=True` on
`GatewayTurns.stream_admitted_agent`. The existing execution boundary validates
session ownership, authority, model and policy before entering `TurnService`.
The service scopes the mode with a context variable and restores it after the
inner generator closes, including failure, early close and cancellation. This
option changes observations only; it never enables a tool or alters approval
consumption. It is independent of `native_terminal_events`, whose tools-disabled
restriction remains in force.

The Loop's tool start, progress and output emission sites call
[`tool_event_frame`](../../src/agent_tool_events.py) before serialization. In
native mode it returns immutable `CoreToolEvent` metadata: a bounded tool name,
optional round and integer exit code, with a lifecycle type (`tool.started`,
`tool.progress`, `tool.completed`, `tool.failed`, `tool.blocked`, or
`tool.awaiting_approval`). Arguments, output, progress tails, screenshots,
receipts, resource identifiers and provider call IDs are excluded. These events
are observations, not complete client tool cards or permission grants. They do
not yet carry a stable per-call identity for replay or parallel correlation.

`CoreApprovalEvent(approval.requested)` is emitted only after the inherited
approval store creates the sealed action. `ToolApprovalDecision.event` exposes
`approval.decided` after admitted owner/session validation and one-use
consumption, including denial. It is a returned fact, not an automatically
broadcast event. Both contain only the opaque approval reference and, for a
decision, its validated choice. A reference alone cannot authorize execution;
continuation still requires the original interactive admission and sealed store.
The typed adapter validates the event shape and drops observations after DONE.

Default Web tool frames retain the original `json.dumps` bytes. Raw `ask_user`,
UI events, retry diagnostics and other legacy families remain SSE, filtered out
by the typed adapter. The direct text composer blocks all tools by default; a server-owned native
profile can now exercise this contract as described below. This is not the
production Web composer or cutover. Rollback requires reverting code
only: no schema, persisted events or production data changes are introduced.

## Composed native tool profile

The existing `TrustedTextTurnSettings` now accepts an immutable `offered_tools`
set, empty by default. This is a server-selected adapter surface, never a
client permission field. A single call to `open_composed_text_events` now runs:

1. Issued admission, loaded owner and account checks before context I/O.
2. Final account/global restrictions plus denial of non-offered native tools.
3. The original owner-scoped inventory, capability classifier and exact intent
   selector, followed by denial of every additional schema in that inventory.
4. Shared context ports, bound request authority and the original immutable
   routed contract resolver.
5. The same `TurnService`, Agent Loop and dispatcher with scoped native tool
   observations; provider ingress and gated answer text remain legacy SSE in
   this tools-enabled profile, translated by the existing typed adapter.

Unknown names and `host_shell` are rejected before context I/O. MCP-prefixed
names are unsupported in this direct profile and MCP execution remains disabled.
A schema cannot remove an owner, account, global, delegated or guide-only denial.
The full Web inventory and UI-aware behavior are unchanged.

[Composed tool tests](../../tests/test_mnerys_gateway_composed_tools.py) prove a
single call reaches the actual Agent Loop and dispatches the immutable notes
read with a bound authority and contract. The model stream and tool result are
synthetic; no production database or network service is used. Tests also cover
browser/bearer/single-user identity, cross-owner rejection before inventory,
unsupported surfaces, account denials and unchanged default no-tools behavior.

The default operation still does not persist user/assistant messages. An explicitly trusted, tools-disabled direct text profile can now persist the user input through the existing context writer and the completed assistant answer through the admitted legacy saver. A failed assistant write becomes a generic terminal failure with no prematurely delivered final frame. When the trusted server supplies an interrupted writer, cancellation, early close, incomplete streams and provider failures save only visible partial text through the existing admitted stopped-response boundary. No partial is saved for an empty answer or after an ambiguous completed-response write failure. This is not an atomic transaction, durable receipt, or production Web cutover. Enabled tools
retain their original authorized effects and storage; do not mistake missing
conversation writes for a read-only guarantee. Native approval requests and controls can now traverse this same composer.
Rich multimodal inputs, full Web editor parity, process/browser resource approvals, durable route attribution and complete Web compatibility remain open.
The native-terminal tools-disabled guard is unchanged. Reverting this code does
not require a schema/data rollback, but it does not undo any authorized tool
effect already performed by a caller. No preview rollout is part of this change.

### Native approval controls and pause state

`ToolApprovalTurnRequest` contains only an opaque approval reference and a
choice (`approve`, `approve_task`, or `deny`). It has no message, credentials,
action arguments or authority fields. The same composer checks the admitted
owner/session and interactive principal through the existing decision service.
API-token principals cannot submit these controls.

The new inspection boundary validates a pending decision without consuming it.
Supported native approvals must retain their sealed digest, backend identity,
originating authority and current tool surface. Workspace, filesystem/process,
browser and active-document contexts unsupported by this profile are rejected
without consuming the card. Current account, owner, global and guide-only
denials remain absolute.

Context preparation restores the originating request for model context only;
it never appends the approval choice as user text or derives new grants from
it. An already stored copy of the originating message is not duplicated. The
composer reuses the sealed authority snapshot and selected-tool set, narrowed
by the current server surface. The exact first action replaces the lexical
required-read operation, preventing the same approved read from being
scheduled a second time.

After successful preparation, the existing store repeats expiry/ownership
checks and consumes the decision once. `approval.decided` precedes the original
Agent Loop's exact-action branch; the unchanged dispatcher still validates and
claims the sealed operation. Denial consumes the card and completes without
context I/O, a model call or tool execution, even if Agent access was revoked.
A context error/cancellation before consumption preserves a live pending card.
Cancellation or runtime failure after consumption never automatically reissues
it; replay is rejected, even when cancellation preceded the first effect.

`TurnService` binds the trusted approval store until inner runtime cleanup,
using the inherited singleton for default Web callers. Native creation and
continuation therefore use the same store, including teacher children; parallel
turns cannot inherit each other's binding. Ordinary new text turns retire any
superseded pending card and preserve its external-context security taint.

A native `approval.requested` followed by DONE now produces
`turn.awaiting_approval` and an in-memory `awaiting_approval` outcome, rather
than claiming task completion. The terminal payload contains only the opaque
reference. Duplicate DONE/late events remain suppressed. This is an additive
process-local state; consumers of this transitional interface must handle it.
The production Web SSE protocol is unchanged.

The inherited wire scopes are preserved (`approve` selects chat-session scope;
`approve_task` selects task scope). The direct profile still does not persist
resolved chat grants or user/assistant messages, so it does not promise a
chat-session bypass across future independent turns or server restarts.
Consumption and external effects are not an atomic transaction or durable
exactly-once receipt. No database migration or preview deployment is included.

[Composed approval tests](../../tests/test_mnerys_gateway_composed_approvals.py)
cover real request → pending approval → resume through the inherited Loop and
dispatcher, with synthetic model responses and a mocked native producer only.
They verify one effect, original authority preservation, denial, replay,
foreign/expired/tampered cards, concurrent attempts, unsupported context,
context failure, cancellation, and approval-store isolation/cleanup. No live
provider or production storage is used.

### Local validation for native approval continuation

The latest focused Mnerys/required-read run passed 1,217 tests (27 skipped).
The final approval-control suite passed 32 tests, and the broader inherited
approval/authority/contract/completion regression passed 196 tests. The
AST/runtime regression passed 715 tests (2 skipped, plus 6 passing subtests);
groups overlap. The legacy exhausted-round approval regression and 23 Node checks also
passed. Full hosted CI and live-model/production acceptance remain outstanding;
the previously documented process, certificate and browser limitations still
prevent treating the broad local suite as approved.

### Local validation for the composed profile

The Mnerys/required-read regression passed 1,183 tests with 27 skips; the
completed native-profile suite passed 24 tests, including actual Loop dispatch
and completion/error/cancel/early-close cleanup. The broader contract/policy/
authority/approval regression passed 1,380 tests, and 23 Node architecture/release
checks passed. These groups overlap. The
unchanged default composer and typed delivery tests also passed before the
broader run. No real provider or production storage was used.

The broad-suite investigation reproduced process-command, containment identity
and certificate-fixture failures on the unmodified base. Browser tests lack a
usable Chromium binary; the install attempt failed with a truncated/invalid
archive. Seven proxy-dependent tests passed after adding SOCKS support to the
isolated test environment only. This does not establish a green full suite or
hosted CI, and no security assertion or production dependency was weakened.

## Legacy frame mapping

| Inherited signal | Typed result | Rules |
| --- | --- | --- |
| Native `CoreTextEvent(message.delta)` after completion filtering; legacy `data: {"delta": "..."}` otherwise | `message.delta` | Only visible, completion-gated answer text; suppress reasoning and unrecognized payloads. |
| Native `CoreTextEvent(message.final)` after completion filtering; legacy `final_response` SSE otherwise | Used for `message.final` | Replaces accumulated visible deltas at successful completion. |
| Native `CoreMetricsEvent` for final Core metrics; legacy provider `usage` SSE remains | `metrics` or `usage` | Retains only nonnegative integer token counts, dropping model IDs, credentials and arbitrary metadata. Distinct event families prevent conflating final totals with provider usage. |
| Native `CoreTerminalEvent(type="turn.completed")` in restricted direct mode; legacy `data: [DONE]` otherwise | `message.final`, then `turn.completed` | Both map to the same terminal ordering; duplicate terminals never emit another result. The native signal is not decoded from SSE. |
| Native `CoreFailureEvent` emitted from the Core completion gate after inherited provider/agent errors; legacy error SSE otherwise | `turn.failed` | Generic failure category and validated HTTP status only; never expose provider details or action receipts. Late DONE cannot override failure. |
| Native `CoreToolEvent` / `CoreApprovalEvent` | Same lifecycle type | Metadata-only observations and opaque sealed approval references; never raw tool arguments or results. |
| Native approval request still pending at DONE | `turn.awaiting_approval` | Preserve the opaque reference and waiting outcome; do not report task completion. |
| EOF without a completion/error marker | `turn.failed` | `incomplete_stream` rather than a false success. |
| Generator close or `CancelledError` while running | Outcome becomes `cancelled` | Preserve cancellation propagation; Python async generator closing cannot yield a final cancellation event. |
| SSE heartbeats, IDs, unknown/tool/approval/reasoning frames | No typed event | Do not interpret raw IDs as durable cursors or expose unfiltered arguments, receipts, hidden reasoning or arbitrary payloads. |

Frames can arrive fragmented or combined; legacy CRLF and comments are handled, and oversized individual frames are rejected. These protections do not replace the future canonical event publisher.

## Security and compatibility constraints

- Admission, ownership and tool policy are still enforced by the composer and admitted execution boundary **before** model/tool access; client-supplied metadata does not create authority.
- The direct text composer defaults to **all tools disabled** and **no conversation writes**, with a separately configured text-only persistence profile using the inherited writers. The server-offered profile supports native tool execution with the same dispatcher; MCP, attachment/document context and workspace/document-bound approval continuations remain unsupported here. Full tool result rendering and client approval UX still require the complete Gateway composition contract.
- The Web transport, SSE bytes, run IDs, stop/finish/reconnect identity and legacy response writer remain unchanged. Do not wire this transitional subset into the production Web route as a substitute for its complete event set.
- The typed `sequence` is **not** a restart-safe event-store sequence. No resume-by-sequence, durable idempotence, revision fencing, or multi-client API is implemented.
- The Agent Core emits native **completion** objects, while its evidence/completion gate now emits native **filtered answer text, final safe metrics and sanitized failure** objects in the opted-in direct mode. Provider ingress and other event families remain SSE; native tool/approval metadata is separately opt-in; some provider usage events still use legacy SSE. All eleven original DONE sites preserve byte-identical default Web SSE. Future work must move *all* permitted event families to native emission and isolate SSE serialization in Web adapters.
- Only a valid, admitted direct turn with all native/MCP tools blocked and no external execution bridge may enable this native completion mode. The teacher takeover wrapper cannot schedule a teacher child from this restricted native path; regular Web behavior remains intact.

## Verification and remaining gates

[Native tool-event tests](../../tests/test_mnerys_agent_core_tool_events.py)
exercise actual required-read branch dispatch, native/default serialization,
completion/teacher wrappers, concurrent formatting isolation, cancellation,
sealed approval requests and owner-checked approve/deny decisions. No real
provider, live database or production tool is used. Local validation passed 1,161 Mnerys/required-read tests (27 skipped),
713 AST/runtime regression tests (2 skipped), and 104 completion/teacher tests;
the groups overlap. Node architecture/release checks passed 23 tests. The local
runtime uses Python 3.12 rather than CI's Python 3.11. The full-suite attempt
was interrupted after multiple failures; it is **not approved**. A separate
serial run stopped at the process-command isolation test, whose identical
failure was reproduced on an unmodified worktree at the base commit. This
runtime also reports nonfunctional bubblewrap. Other full-suite failures have
not been classified; hosted CI and live-model/Web parity remain required.


[Typed event tests](../../tests/test_mnerys_gateway_turn_events.py) cover native/legacy text, metrics, failures and terminal ordering, result snapshots, redaction, duplicates, interruptions and size limits. [Real Agent Core tests](../../tests/test_mnerys_agent_core_native_terminal.py) prove native outputs originate after the actual completion gate, preserve the teacher restrictions and keep default Web-compatible SSE bytes. The provider responses are synthetic; no live-model proof is claimed. The [direct composer tests](../../tests/test_mnerys_gateway_turn_composer.py) verify ownership before context I/O and the no-HTTP Agent Loop handoff, including typed success/failure/cancellation.

The [M2.1 exit gate](M2_1_EXIT_GATE.md) remains **OPEN** for a full-featured admitted composer, native typed events and finalization/persistence, Web/direct parity, real model execution and full hosted CI. This limited converter is an incremental acceptance proof, not a release or deploy approval.

## Opt-in legacy-backed text conversation writes

The [direct persistence adapter](../../src/gateway_text_persistence.py) wraps
the existing typed event stream and owner-admitted assistant writer. The
shared context service appends only a validated text user message when
explicitly configured by a trusted server caller. A completed assistant answer
is saved before its `turn.completed` notification; no duplicate terminal
frame can create another message in the same iterator. Persistence exceptions
become generic `turn.failed` events without exposing exception messages.
Without the separately trusted interrupted writer, a failure leaves the user
message but does not persist an assistant partial. With that writer configured,
only filtered visible partial content is retained.

The default and tool/approval-enabled direct profiles retain their previous
non-persisting behavior. There is no atomic whole-turn transaction, durable
run identifier, replay receipt, or production SQL-backed event store. The
acceptance tests are in
[`test_mnerys_gateway_text_persistence.py`](../../tests/test_mnerys_gateway_text_persistence.py).

## Interrupted direct text delivery (opt-in)

A trusted direct text persistence caller may supply the existing admitted
interrupted writer. For a nonempty visible answer segment, early close and
cancellation attempt a stopped partial save during generator cleanup, preserving
the original interruption even when the legacy writer fails. Explicit provider
and incomplete-stream failures also attempt a partial save before publishing
their typed `turn.failed` event. Invalid ownership or an ambiguous writer
failure never triggers an automatic retry. Empty/reasoning-only streams do not
create stopped assistant messages.

The persistence wrapper buffers the translated `message.final` signal until
the legacy completed-response saver succeeds. A failed completed save emits
only a generic `persistence_error` terminal event, without sending an
uncommitted final response or appending a second stopped reply. Event sequences
remain process-local and contiguous for delivered events; there are no durable
turn identifiers, retention guarantees, cross-worker resume cursors or
atomic user/assistant writes.

The new regression cases in
[`test_mnerys_gateway_text_persistence.py`](../../tests/test_mnerys_gateway_text_persistence.py)
cover early-close history continuation, provider and incomplete-stream failures,
reasoning exclusion, write errors, late owner changes, duplicate terminals and
final-event ordering. The production Web serializer and writer paths are not
migrated by this feature.

## Persisted direct text confirmation (SQLite acceptance)

The opt-in typed direct-completion profile no longer trusts a legacy
assistant writer merely returning without an exception. It checks that the
same managed session gained exactly one new role-matched message and
a fresh `_db_id` from the inherited post-commit writer. The context
builder invokes the corresponding user-write check before doing further
context retrieval or model invocation. A missing receipt becomes a
sanitized `turn.failed` (`persistence_error`) event; no final completion
is acknowledged. A blocked or deleted session cannot create a false
successful turn.

These checks are covered by disposable SQLite and PostgreSQL 17 databases
using the original SQLAlchemy models and SessionManager, plus synthetic model
output.
The default direct profile, existing production Web SSE path, and
tool/approval continuation semantics remain unchanged. No durable
cross-process completion receipt, SQL-backed event log or transaction fence
has been implemented.

## Session-bound workspace approval observations

The direct text profile can now pass a workspace already selected by the
trusted server adapter and matching the admitted session's persisted `cwd`
to the *same* authority, contract and Agent Loop. Existing sealed native
backend approvals can be resumed for exactly that workspace, without
constructing a grant from approval-button input. The typed stream reports the
same `approval.decided` and tool lifecycle events, and the inherited
dispatcher claims the first effect at most once.

Workspace filesystem effects now require a separately validated, original
sealed filesystem operation; the generic workspace setting alone grants no
filesystem mutations. Active-document approvals separately require
owner/session/version/digest checks. Process/browser/remote resource bindings
still fail closed and remain unconsumed. The current account/owner/tool policy,
workspace path and pending approval are rechecked before consumption.
[Isolated tests](../../tests/test_mnerys_gateway_workspace_approvals.py)
cover the native Agent Loop round trip, real dispatcher handoff, negative
workspace changes, symlink retargeting, cancellations and policy revocations.
This is process-local functionality, not a durable approval record or a
Web-versus-direct acceptance claim.

## Admitted active-document turn profile

A trusted server caller may select one active document by ID. Before context
assembly, the Gateway resolves it from the existing SQL store using the
authenticated owner, exact current session and its current database owner,
active state, archive status, version and content digest. The frozen document snapshot is forwarded to the
same Agent Loop and its existing untrusted-document prompt handling.
The default text profile remains without conversation writes.

For a previously sealed native `edit_document`, `update_document` or
`suggest_document` approval, the direct composer also requires the
previously bound `BoundOwnedOperation` and unchanged SQL document identity
before and after asynchronous context preparation. It never invents an
approval from a selected editor tab or a button choice; it forwards the
existing exact approval and original request authority. Document-denial
controls remain available even if the editor has been closed.

[SQLite/PostgreSQL 17 acceptance](../../tests/test_mnerys_gateway_document_approvals.py)
covers sealed document IDs, version/digest conflicts, cross-user/session
denial, archiving/removal, account restrictions, and a real inherited
dispatcher handoff with no production effect. Unbound or remote filesystem, process and browser approvals, as well as
implicit cross-session rebinding, remain unsupported;
full Web editor and response-persistence parity is still open.

## Exact native filesystem approval routing

The in-process direct composer can now resume an already sealed native
`read_file`, `write_file`, `edit_file`, `apply_patch`, `ls`,
`glob` or `grep` operation within the admitted session workspace.
The server-selected workspace, sealed root, original request ID, exact
tool arguments and observed resource identities are verified before the
approval is consumed. The inherited dispatcher performs execution-time
validation again. A lexical classifier's `unknown` continuation is
routed to `shell_files` **only after** the filesystem approval preflight
succeeds; the offered contract remains narrowed to the exact sealed tool
and can never restore an additional shell/process family.

The same typed approval and tool lifecycle observations are produced.
A tool refusal is reported as a failed tool, not an authorized effect.
This is especially important on public deployments: a user approving a
card cannot override the existing filesystem administrator restriction.
A denial does not require the workspace to remain available.

[Filesystem-specific acceptance](../../tests/test_mnerys_gateway_filesystem_approvals.py)
uses only throwaway pytest workspaces and synthetic models, with the
inherited Agent Loop and dispatcher. No filesystem work against the real
workspace, remote credentials, durable event replay or Web parity has
been validated.

## Opt-in fallback in the same admitted Agent Loop

The direct composer now accepts a trusted `enable_model_fallback` flag
and a server-selected `allowed_fallback_models` set. It uses the existing
owner-scoped foreground model policy and existing Agent Loop candidate
routing; the primary remains the model in the admitted session. User
preferences must also enable the fallback. Without both opt-ins, the
direct route stays strict. Every fallback candidate is checked against
the explicit adapter allowlist. Provider headers remain internal to
the model runtime and are never included in filtered typed events.

The inherited Loop performs per-model context shaping and respects
eligible HTTP status failures, the configured empty-output fallback
policy, and the rule that visible first-provider output prevents a
silent retry on another model. Cancellation closes the same async generator,
without creating a second model execution process.

The typed Gateway consumer now receives a restricted `model.route.selected` observation generated by the **original fallback candidate selector**, not provider-authored SSE. It exposes only the selected model ID, answering candidate model ID, candidate index and round; it does not disclose URLs, headers, diagnostic reasons, provider aliases or a final save receipt. Unknown provider metadata remains filtered.
[Direct tests](../../tests/test_mnerys_gateway_direct_fallback.py)
verify a synthetic real-Loop fallback to an allowed model, blocked and
noneligible routes, account scoping, partial-output pinning and cleanup.
No real provider was used, and Web-versus-direct event parity remains
unverified.

## Direct text attachment context without new authority

One restricted non-persisting direct text profile can now select owner-scoped
uploaded text references, with at most four small files. Selection IDs are
untrusted, so the trusted upload manifest/resolver confirms current owner,
path confinement, name/MIME, stored SHA-256 and actual file bytes before
the inherited ChatHandler extracts text. The snapshot is checked again
after async context preparation.

The model receives a guarded `untrusted_context_message` containing the
extracted file text followed by the *original user's request*, keeping
file instructions out of the user command and request authority. File text,
upload paths and checksums are not emitted in typed events or granted to
tools. Direct tool use, approval continuation and message persistence remain
unsupported for this attachment profile. Default Web preprocessing is
unchanged; direct attachment preprocessing does not fetch URLs or probe
vision models.

[Isolated direct attachment tests](../../tests/test_mnerys_gateway_direct_attachments.py)
verify no conversation writes, cross-owner denial, altered files,
prompt-guard escaping and compatibility with the existing Web context
ports. Images, audio, rich documents and Web parity remain open.

## Native source-owned model route selection

The original fallback selector invokes an internal synchronous observer
when a non-primary candidate first commits substantive output. The
`CoreModelRouteEvent` passes through the existing completion and teacher
wrappers and becomes one `model.route.selected` observation for the direct
typed consumer. Its payload includes only the selected model, answering
candidate model, index and Agent round; there are no credentials, endpoint
URLs, failure diagnostics or raw provider aliases.

Upstream providers cannot mint this native event by emitting SSE. The
original model selector now discards provider-originated `fallback` control
frames and still emits its own original fallback SSE for real switches.
The non-typed and Web streams preserve their existing event shape. No new
tool grants, retries, conversation writers or durable event cursors exist
in this slice. The observation describes a selected route, not a final
persisted answering model. Real-model and Web parity acceptance remain open.

## Shared Web and typed process-local run lifecycle

A trusted Python caller may now detach a direct composed typed turn through
`GatewayTurns.open_composed_detached_text_events`. Both typed and existing
SSE runs use the single [Agent run manager](../../src/agent_runs.py),
including run replacement, exact-run Stop and a bounded replay buffer.
The typed path adds a distinct event representation to that **same**
registry, not another executor, scheduler or event store. A
`TypedRunEvent` includes an opaque run identity, a run-scoped replay cursor
and the original filtered `TurnEvent`. Resuming checks the issued
session/owner and the exact current typed run; a stale cursor, different
run, foreign owner or SSE/typed representation mismatch fails closed.

The manager holds terminal events until the composer/persistence generator
has completely unwound; it does not report success before the inherited
assistant saver confirms its new receipt. Disconnecting from a detached
subscription does not cancel the Agent; exact-run Stop closes upstream
execution and reuses the original interrupted partial writer. A stop or
replacement **before the task starts** is also terminalized immediately
so it cannot appear permanently active. Provider errors remain generic,
and no provider body or credential is placed into replay.

[Lifecycle acceptance](../../tests/test_mnerys_gateway_detached_typed_lifecycle.py)
covers the direct model path and inherited writers using synthetic
providers and disposable session data. The buffer and cursor are
**not durable** or a promise of replay after restart; the Web endpoint
has not switched to typed delivery, and no independently authenticated
CLI, TUI or Telegram adapter is ready.

## Filtered plain-text adapter to inherited Web SSE

The strict [Web compatibility serializer](../../src/gateway_web_compat_events.py)
maps only admitted text, message receipt, sanitized failure, numeric usage
and Core-owned model route observations to browser-understood SSE frames.
A trusted Gateway preview function selects the **same composed typed turn**
and the existing assistant writer; it never creates a second Agent Loop.

For this preview, the original writer confirms an assistant `_db_id` before
the optional `message.saved` event is emitted. The confirmed receipt comes
before the canonical `message.final`; `turn.completed` still follows the
writer. The default typed event profile is unchanged. SSE frames are JSON
encoded, so untrusted text cannot insert `event:` or `id:` directives.
Unknown tool/approval/editor events are rejected, not guessed into a
button or document edit card. Errors remain generic, without sensitive
provider or writer diagnostics.

The original run-scoped replay cursors may be serialized as SSE IDs for
trusted detached typed subscriptions, but no new replay store or remote
transport exists. A late replay that starts after the saved-message event
must obtain `prior_saved_receipt` from the exact original run buffer:
replayed terminal frames without a previously emitted receipt fail closed. A replay cursor is not a database receipt, and a resumed
reader may have already acknowledged the saved receipt in an earlier page.
See the [no-HTTP acceptance tests](../../tests/test_mnerys_gateway_web_compat_events.py).
The existing Web endpoint remains unchanged; broad UI and real-provider
parity are still pending.

## Web preview of native tool status without executable UI grants

For the first bounded native surface, the no-HTTP Web compatibility layer
accepts the original Core's **sanitized tool timeline metadata** for one
server-offered `manage_notes` tool. The projected browser events contain
only the safe tool name, empty command/output and a generic progress or
validated success/failure marker. They do not expose tool arguments,
results, model-provided text, filesystem resources, private stdout,
document actions or `ui_event`. A matching start is required before each
progress/completion; overlapping or incomplete tool sequences fail closed.

The only approved control-plane presentation is
`approval.decided` → `tool_approval_resolved`, after the existing
sealed approval store has already consumed the decision. There is
**no** `approval.requested` → browser-card translation in this profile:
creating a UI action requires the full owner/session-bound sealed card
contract and separate acceptance. No model/tool permission can be granted
by the renderer, no tool is retried, and the production Web SSE surface
is not changed. [No-HTTP tests](../../tests/test_mnerys_gateway_web_tool_status.py)
verify both positive status rendering and negative authorization/UI
escalation.

## Sealed pending-review projection (still in-process)

The filtered native stream can now carry a reference to a pending review
through `approval.requested`, a genuine `tool.awaiting_approval` status and
`turn.awaiting_approval`. Only the original
[ToolApprovalStore](../../src/tool_approvals.py) can provide the complete
card data, via the trusted owner/session-checked
[review reader](../../src/gateway_web_approval_cards.py). The reference
alone is not a credential or a source of selectable decisions.

The no-HTTP serializer keeps the card **unpublished** until the source
has fully unwound, then rereads the same sealed record and compares its
digest and complete public payload before emitting the browser's
`ask_user` + `[DONE]`. It never converts an unexecuted approval gate into
a successful `tool_output`; it does not fabricate a button on an expired,
tampered, consumed or foreign ID. The existing `ToolApprovalTurnRequest`
path remains the only one-use consumption mechanism for a local review.

This is a presentation adapter only. It does not expose the sealed
authority or full raw resource operations, and no Web endpoint, durable
event store, remote authentication or UI button transport was added.
See [approval-card acceptance](../../tests/test_mnerys_gateway_web_approval_cards.py).
