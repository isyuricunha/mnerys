"""M2.1 transitional, transport-neutral view of inherited Agent Loop events.

The inherited Agent Loop still emits SSE text. This adapter translates a
bounded, explicitly allowed subset into typed, in-process events for the
direct text-only composer. It is NOT the proposed durable TurnEvent schema,
a complete tool/approval serializer, or a replacement for the Web SSE path.

Do not expose this module as a remote event API without owner admission,
tool-result filtering and a real server-issued turn ID/event sequence.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
import json
from typing import Any, Literal

from src.agent_core_events import (
    CoreTerminalEvent, CoreTextEvent, CoreMetricsEvent, CoreFailureEvent, CoreNativeEvent,
)


TurnState = Literal["pending", "running", "completed", "failed", "cancelled"]

_MAX_LEGACY_FRAME_CHARS = 1_048_576


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """A filtered, process-local event (seq is not a replay cursor)."""

    sequence: int
    type: str
    payload: dict[str, Any]


@dataclass(slots=True)
class TurnOutcome:
    """Result snapshot for a single in-process stream; never persisted."""

    state: TurnState = "pending"
    content: str = ""
    last_sequence: int = 0
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class TypedTurnDelivery:
    """Direct-consumer event stream and its evolving completion snapshot."""

    events: AsyncIterator[TurnEvent]
    outcome: TurnOutcome


def _frame_fields(frame: str) -> tuple[str, str]:
    event_type = "message"
    payload_lines: list[str] = []
    for line in frame.split("\n"):
        if not line or line.startswith(":"):
            continue
        key, delimiter, value = line.partition(":")
        if not delimiter:
            continue
        if value.startswith(" "):
            value = value[1:]
        if key == "event":
            event_type = value
        elif key == "data":
            payload_lines.append(value)
        # Deliberately drop SSE id/retry/extensions: no durable run cursor.
    return event_type, "\n".join(payload_lines)


def _safe_status(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if type(status) is int and 400 <= status <= 599:
        return status
    failure = payload.get("failure")
    if isinstance(failure, dict):
        status = failure.get("status")
        if type(status) is int and 400 <= status <= 599:
            return status
    return None


def _safe_metrics(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: value for key in ("input_tokens", "output_tokens", "total_tokens")
        if type(value := payload.get(key)) is int and value >= 0
    }


async def translate_legacy_turn_events(
    frames: AsyncIterator[str | CoreNativeEvent],
    outcome: TurnOutcome,
) -> AsyncIterator[TurnEvent]:
    """Translate safe legacy SSE signals and terminate a process-local turn.

    Treat provider text as untrusted. Only visible message text, sanitized
    numeric usage and generic terminal outcomes are available to this first
    direct-text consumer. Tool arguments/results, hidden reasoning, SSE ids,
    provider diagnostics and unknown payloads never enter typed events.

    Closing this iterator explicitly closes the inherited Agent Loop before
    its bridge and contract contextvars are reset.
    """
    if not isinstance(outcome, TurnOutcome):
        raise TypeError("A server-created turn outcome is required")
    if outcome.state != "pending":
        raise ValueError("Turn outcomes cannot be reused")

    outcome.state = "running"
    buffer = ""
    pending_cr = False
    deltas: list[str] = []
    final_response: str | None = None

    def event(kind: str, payload: dict[str, Any]) -> TurnEvent:
        outcome.last_sequence += 1
        return TurnEvent(outcome.last_sequence, kind, payload)

    try:
        async with aclosing(frames) as source:
            async for chunk in source:
                if isinstance(chunk, (CoreTextEvent, CoreMetricsEvent, CoreFailureEvent)):
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before native Core event")
                    if outcome.state in ("completed", "failed"):
                        continue
                    if isinstance(chunk, CoreTextEvent):
                        if type(chunk.text) is not str:
                            raise ValueError("Invalid native Core text")
                        if chunk.type == "message.delta":
                            deltas.append(chunk.text)
                            yield event("message.delta", {"text": chunk.text})
                        elif chunk.type == "message.final":
                            final_response = chunk.text
                        else:
                            raise ValueError("Unknown native Core text event")
                    elif isinstance(chunk, CoreMetricsEvent):
                        if chunk.type != "metrics":
                            raise ValueError("Invalid native Core metrics")
                        fields = {
                            key: value for key in (
                                "input_tokens", "output_tokens", "total_tokens"
                            )
                            if type(value := getattr(chunk, key)) is int and value >= 0
                        }
                        yield event("metrics", fields)
                    else:
                        if chunk.type != "turn.failed" or chunk.code not in (
                            "provider_error", "agent_terminal",
                        ):
                            raise ValueError("Invalid native Core failure")
                        status = chunk.status if (
                            type(chunk.status) is int and 400 <= chunk.status <= 599
                        ) else None
                        outcome.state = "failed"
                        outcome.failure_code = chunk.code
                        yield event("turn.failed", {
                            "code": chunk.code, "status": status,
                        })
                    continue
                if isinstance(chunk, CoreTerminalEvent):
                    # Core-native completion for admitted, tools-disabled
                    # direct turns: no SSE decode is needed for this terminal.
                    if chunk.type != "turn.completed":
                        raise ValueError("Invalid Agent Core terminal signal")
                    if outcome.state in ("completed", "failed"):
                        continue
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before Core terminal")
                    outcome.content = (
                        final_response if final_response is not None
                        else "".join(deltas)
                    )
                    outcome.state = "completed"
                    yield event("message.final", {"text": outcome.content})
                    yield event("turn.completed", {})
                    continue
                if not isinstance(chunk, str):
                    raise ValueError("Invalid inherited stream chunk")
                # Canonicalize legacy CRLF frames without changing the model
                # stream or the Web transport's original bytes.
                normalized = ("\r" if pending_cr else "") + chunk
                pending_cr = normalized.endswith("\r")
                if pending_cr:
                    normalized = normalized[:-1]
                buffer += normalized.replace("\r\n", "\n").replace("\r", "\n")
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    if len(frame) > _MAX_LEGACY_FRAME_CHARS:
                        raise ValueError("Inherited event frame exceeds safety limit")
                    event_name, raw = _frame_fields(frame)
                    if not raw and event_name != "error":
                        continue
                    if outcome.state in ("completed", "failed"):
                        # Some providers emit DONE twice or an error after DONE.
                        continue
                    if event_name == "error":
                        try:
                            error = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            error = None
                        status = _safe_status(error)
                        outcome.failure_code = "provider_error"
                        outcome.state = "failed"
                        yield event("turn.failed", {
                            "code": "provider_error", "status": status,
                        })
                        continue
                    if raw == "[DONE]":
                        outcome.content = (
                            final_response if final_response is not None
                            else "".join(deltas)
                        )
                        outcome.state = "completed"
                        yield event("message.final", {"text": outcome.content})
                        yield event("turn.completed", {})
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # Do not reflect malformed provider data.
                        continue
                    if not isinstance(data, dict):
                        continue
                    kind = data.get("type")
                    if kind == "agent_terminal":
                        metadata = data.get("data")
                        status = _safe_status(metadata)
                        outcome.failure_code = "agent_terminal"
                        outcome.state = "failed"
                        yield event("turn.failed", {
                            "code": "agent_terminal", "status": status,
                        })
                    elif kind == "final_response" and isinstance(data.get("content"), str):
                        final_response = data["content"]
                    elif kind == "usage":
                        yield event("usage", _safe_metrics(data.get("data")))
                    elif (
                        kind in (None, "delta")
                        and isinstance(data.get("delta"), str)
                        and not data.get("thinking")
                        and not data.get("reasoning")
                        and data.get("channel") not in ("analysis", "thinking", "reasoning")
                        and "reasoning_content" not in data
                    ):
                        value = data["delta"]
                        deltas.append(value)
                        yield event("message.delta", {"text": value})
                    # Unknown tool/approval/reasoning/provider events are not
                    # surfaced until the full filtered Gateway contract exists.
                if len(buffer) > _MAX_LEGACY_FRAME_CHARS:
                    raise ValueError("Inherited event frame exceeds safety limit")
        if outcome.state == "running":
            outcome.state = "failed"
            outcome.failure_code = "incomplete_stream"
            yield event("turn.failed", {"code": "incomplete_stream", "status": None})
    except (asyncio.CancelledError, GeneratorExit):
        if outcome.state == "running":
            outcome.state = "cancelled"
            outcome.failure_code = None
        raise
    except BaseException:
        if outcome.state == "running":
            outcome.state = "failed"
            outcome.failure_code = "stream_exception"
        raise
