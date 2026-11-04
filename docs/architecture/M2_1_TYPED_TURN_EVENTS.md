# M2.1 typed direct-turn events — transitional adapter

**Status: PARTIAL — native terminal, completion-gated visible text, sanitized final metrics and generic failures now reach the admitted, tools-disabled direct composer. A separate admitted runtime option now emits filtered native tool metadata and sealed approval references; provider ingress and production Web still use inherited SSE.**

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
Attachment/document context, workspace/browser-bound approval continuation,
model fallback and full Web parity remain open.
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
An interrupted stream or provider failure leaves the user message but does
not persist an assistant partial in this restricted profile.

The default and tool/approval-enabled direct profiles retain their previous
non-persisting behavior. There is no atomic whole-turn transaction, durable
run identifier, replay receipt, or independently tested SQL backend. The
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
