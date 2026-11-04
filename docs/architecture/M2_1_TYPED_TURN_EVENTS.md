# M2.1 typed direct-turn events — transitional adapter

**Status: PARTIAL — native terminal, completion-gated visible text, sanitized final metrics and generic failures now reach the admitted, tools-disabled direct composer. Tool/approval events, provider ingress and production Web still use inherited SSE.**

This is a bounded implementation record for the [M2.1 exit gate](M2_1_EXIT_GATE.md), not the final [durable Gateway event protocol](GATEWAY_AGENT_CONTRACT.md#event-schema--proposal).

## Supported flow

1. The trusted Python caller uses [GatewayTurns.open_composed_text_events](../../src/gateway_turns.py) with the **issued session admission**, server-loaded session, trusted context ports and server-selected [text-turn settings](../../src/gateway_turn_composer.py).
2. On first consumption, the composer validates owner/account permission, builds the shared prompt and final **tools-disabled** authority, and starts the inherited Agent Loop through [TurnService](../../src/turn_service.py).
3. The [Agent Core](../../src/agent_loop.py) can emit [CoreTerminalEvent](../../src/agent_core_events.py) directly on completion. Its [completion/evidence gate](../../src/agent_runtime/completion.py) now also produces native, post-filter **visible text, safe final usage counts and generic failure events**. The teacher decorator preserves these signals and prevents restricted direct turns from launching child executions. The [Gateway event adapter](../../src/gateway_turn_events.py) consumes the native objects directly and still interprets other inherited SSE frames; nested generators close before bridge/contract context variables are released.
4. Consumers can read events and outcome directly in Python, **without importing a Web route**. The production [Web adapter](../../routes/chat_routes.py) remains on its existing SSE transport, detached-run registry, approval and persistence paths.

This does **not** create another Agent Loop, an HTTP endpoint, a remote authorization mechanism or a persistence writer. A stream returned by `open_composed_text_events` starts lazily; a forged session is rejected when consumed, before context I/O.

## In-process data contracts

[TurnEvent](../../src/gateway_turn_events.py) is a frozen typed envelope: `sequence` (1-based, local to this stream), `type` (a permitted event name) and filtered `payload`. It does not contain a server-issued `turn_id`, a durable revision or a replay cursor.

[TurnOutcome](../../src/gateway_turn_events.py) is an evolving **in-memory** result snapshot with `state` (`pending`, `running`, `completed`, `failed` or `cancelled`), user-visible `content` on completion, last local `sequence` and a generic `failure_code`. It is not an acknowledgement of database persistence or a cross-process transaction.

[TypedTurnDelivery](../../src/gateway_turn_events.py) couples the async event iterator and its result snapshot.

## Legacy frame mapping

| Inherited signal | Typed result | Rules |
| --- | --- | --- |
| Native `CoreTextEvent(message.delta)` after completion filtering; legacy `data: {"delta": "..."}` otherwise | `message.delta` | Only visible, completion-gated answer text; suppress reasoning and unrecognized payloads. |
| Native `CoreTextEvent(message.final)` after completion filtering; legacy `final_response` SSE otherwise | Used for `message.final` | Replaces accumulated visible deltas at successful completion. |
| Native `CoreMetricsEvent` for final Core metrics; legacy provider `usage` SSE remains | `metrics` or `usage` | Retains only nonnegative integer token counts, dropping model IDs, credentials and arbitrary metadata. Distinct event families prevent conflating final totals with provider usage. |
| Native `CoreTerminalEvent(type="turn.completed")` in restricted direct mode; legacy `data: [DONE]` otherwise | `message.final`, then `turn.completed` | Both map to the same terminal ordering; duplicate terminals never emit another result. The native signal is not decoded from SSE. |
| Native `CoreFailureEvent` emitted from the Core completion gate after inherited provider/agent errors; legacy error SSE otherwise | `turn.failed` | Generic failure category and validated HTTP status only; never expose provider details or action receipts. Late DONE cannot override failure. |
| EOF without a completion/error marker | `turn.failed` | `incomplete_stream` rather than a false success. |
| Generator close or `CancelledError` while running | Outcome becomes `cancelled` | Preserve cancellation propagation; Python async generator closing cannot yield a final cancellation event. |
| SSE heartbeats, IDs, unknown/tool/approval/reasoning frames | No typed event | Do not interpret raw IDs as durable cursors or expose unfiltered arguments, receipts, hidden reasoning or arbitrary payloads. |

Frames can arrive fragmented or combined; legacy CRLF and comments are handled, and oversized individual frames are rejected. These protections do not replace the future canonical event publisher.

## Security and compatibility constraints

- Admission, ownership and tool policy are still enforced by the composer and admitted execution boundary **before** model/tool access; client-supplied metadata does not create authority.
- The current direct text composer intentionally has **all native/MCP tools disabled**, no attachment/document operations and **no conversation writes**. Tool/approval event serialization must wait for the complete permission-filtered Gateway event contract.
- The Web transport, SSE bytes, run IDs, stop/finish/reconnect identity and legacy response writer remain unchanged. Do not wire this transitional subset into the production Web route as a substitute for its complete event set.
- The typed `sequence` is **not** a restart-safe event-store sequence. No resume-by-sequence, durable idempotence, revision fencing, or multi-client API is implemented.
- The Agent Core emits native **completion** objects, while its evidence/completion gate now emits native **filtered answer text, final safe metrics and sanitized failure** objects in the opted-in direct mode. Provider ingress and tool/approval/other events remain SSE; some provider usage events still use legacy SSE. All eleven original DONE sites preserve byte-identical default Web SSE. Future work must move *all* permitted event families to native emission and isolate SSE serialization in Web adapters.
- Only a valid, admitted direct turn with all native/MCP tools blocked and no external execution bridge may enable this native completion mode. The teacher takeover wrapper cannot schedule a teacher child from this restricted native path; regular Web behavior remains intact.

## Verification and remaining gates

[Typed event tests](../../tests/test_mnerys_gateway_turn_events.py) cover native/legacy text, metrics, failures and terminal ordering, result snapshots, redaction, duplicates, interruptions and size limits. [Real Agent Core tests](../../tests/test_mnerys_agent_core_native_terminal.py) prove native outputs originate after the actual completion gate, preserve the teacher restrictions and keep default Web-compatible SSE bytes. The provider responses are synthetic; no live-model proof is claimed. The [direct composer tests](../../tests/test_mnerys_gateway_turn_composer.py) verify ownership before context I/O and the no-HTTP Agent Loop handoff, including typed success/failure/cancellation.

The [M2.1 exit gate](M2_1_EXIT_GATE.md) remains **OPEN** for a full-featured admitted composer, native typed events and finalization/persistence, Web/direct parity, real model execution and full hosted CI. This limited converter is an incremental acceptance proof, not a release or deploy approval.
