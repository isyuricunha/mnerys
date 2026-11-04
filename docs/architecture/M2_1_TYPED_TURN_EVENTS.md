# M2.1 typed direct-turn events — transitional adapter

**Status: PARTIAL — verified in-process for the no-tools, non-persisting direct text composer. The inherited Agent Loop and production Web stream still use SSE frames.**

This is a bounded implementation record for the [M2.1 exit gate](M2_1_EXIT_GATE.md), not the final [durable Gateway event protocol](GATEWAY_AGENT_CONTRACT.md#event-schema--proposal).

## Supported flow

1. The trusted Python caller uses [GatewayTurns.open_composed_text_events](../../src/gateway_turns.py) with the **issued session admission**, server-loaded session, trusted context ports and server-selected [text-turn settings](../../src/gateway_turn_composer.py).
2. On first consumption, the composer validates owner/account permission, builds the shared prompt and final **tools-disabled** authority, and starts the inherited Agent Loop through [TurnService](../../src/turn_service.py).
3. The [compatibility translator](../../src/gateway_turn_events.py) incrementally interprets its existing SSE frames into **filtered, process-local events** and a separate result snapshot. The translator closes its wrapped generator before releasing bridge/contract context variables.
4. Consumers can read events and outcome directly in Python, **without importing a Web route**. The production [Web adapter](../../routes/chat_routes.py) remains on its existing SSE transport, detached-run registry, approval and persistence paths.

This does **not** create another Agent Loop, an HTTP endpoint, a remote authorization mechanism or a persistence writer. A stream returned by `open_composed_text_events` starts lazily; a forged session is rejected when consumed, before context I/O.

## In-process data contracts

[TurnEvent](../../src/gateway_turn_events.py) is a frozen typed envelope: `sequence` (1-based, local to this stream), `type` (a permitted event name) and filtered `payload`. It does not contain a server-issued `turn_id`, a durable revision or a replay cursor.

[TurnOutcome](../../src/gateway_turn_events.py) is an evolving **in-memory** result snapshot with `state` (`pending`, `running`, `completed`, `failed` or `cancelled`), user-visible `content` on completion, last local `sequence` and a generic `failure_code`. It is not an acknowledgement of database persistence or a cross-process transaction.

[TypedTurnDelivery](../../src/gateway_turn_events.py) couples the async event iterator and its result snapshot.

## Legacy frame mapping

| Inherited signal | Typed result | Rules |
| --- | --- | --- |
| `data: {"delta": "..."}` | `message.delta` | Visible text only; suppress thinking/reasoning channels and unknown event families. |
| `data: {"type": "final_response", "content": "..."}` | Used for `message.final` | Supersedes accumulated visible deltas on successful completion. |
| `data: {"type": "usage", ...}` | `usage` | Retains nonnegative integer input/output/total token counts only, excluding provider secrets and arbitrary fields. |
| `data: [DONE]` | `message.final`, then `turn.completed` | Marks completion once; duplicate DONE does not emit another terminal result. |
| `event: error` or agent-terminal failure | `turn.failed` | Generic error code and validated HTTP status only; never return provider diagnostics. A late DONE cannot turn failure into success. |
| EOF without a completion/error marker | `turn.failed` | `incomplete_stream` rather than a false success. |
| Generator close or `CancelledError` while running | Outcome becomes `cancelled` | Preserve cancellation propagation; Python async generator closing cannot yield a final cancellation event. |
| SSE heartbeats, IDs, unknown/tool/approval/reasoning frames | No typed event | Do not interpret raw IDs as durable cursors or expose unfiltered arguments, receipts, hidden reasoning or arbitrary payloads. |

Frames can arrive fragmented or combined; legacy CRLF and comments are handled, and oversized individual frames are rejected. These protections do not replace the future canonical event publisher.

## Security and compatibility constraints

- Admission, ownership and tool policy are still enforced by the composer and admitted execution boundary **before** model/tool access; client-supplied metadata does not create authority.
- The current direct text composer intentionally has **all native/MCP tools disabled**, no attachment/document operations and **no conversation writes**. Tool/approval event serialization must wait for the complete permission-filtered Gateway event contract.
- The Web transport, SSE bytes, run IDs, stop/finish/reconnect identity and legacy response writer remain unchanged. Do not wire this transitional subset into the production Web route as a substitute for its complete event set.
- The typed `sequence` is **not** a restart-safe event-store sequence. No resume-by-sequence, durable idempotence, revision fencing, or multi-client API is implemented.
- The inherited Agent Loop still creates SSE before conversion. A later implementation must emit typed events natively from the shared Core and place SSE serialization exclusively in client adapters.

## Verification and remaining gates

[Typed event tests](../../tests/test_mnerys_gateway_turn_events.py) cover fragmented frames, terminal ordering, result snapshots, sanitization, duplicates, provider errors, interrupted streams, exception propagation and size limits. The [direct composer tests](../../tests/test_mnerys_gateway_turn_composer.py) verify ownership before context I/O and the no-HTTP Agent Loop handoff, including typed success/failure/cancellation.

The [M2.1 exit gate](M2_1_EXIT_GATE.md) remains **OPEN** for a full-featured admitted composer, native typed events and finalization/persistence, Web/direct parity, real model execution and full hosted CI. This limited converter is an incremental acceptance proof, not a release or deploy approval.
