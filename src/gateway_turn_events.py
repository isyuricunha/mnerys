"""M2.1 transitional, transport-neutral view of inherited Agent Loop events.

The inherited Agent Loop still emits SSE text. This adapter translates a
bounded, explicitly allowed subset into typed, in-process events for the
direct text-only composer. It is NOT the proposed durable TurnEvent schema,
a complete tool-result/approval-card serializer, or a replacement for the Web SSE path.

Do not expose this module as a remote event API without owner admission,
tool-result filtering and a real server-issued turn ID/event sequence.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

from src.agent_core_events import (
    CoreTerminalEvent, CoreTextEvent, CoreMetricsEvent, CoreFailureEvent, CoreNativeEvent,
    CoreToolEvent, CoreApprovalEvent, CoreModelRouteEvent,
)


TurnState = Literal["pending", "running", "awaiting_approval", "completed", "failed", "cancelled"]

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
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class TypedRunEvent:
    """A process-local replay item, not a durable Gateway event cursor."""

    run_id: str
    cursor: str
    event: TurnEvent


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
        key: value for key in (
            "input_tokens", "output_tokens", "total_tokens",
            "cached_tokens", "reasoning_tokens",
        )
        if type(value := payload.get(key)) is int and value >= 0
    }


_WEB_SOURCE_MAX_ITEMS = 50
_WEB_SOURCE_MAX_CHARS = 4096
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]{1,2048}")

# Frame families a trusted client adapter may pass through unchanged after
# JSON parsing. They are the browser's existing UI vocabulary; admission,
# approval and persistence authority never come from any of them.
_CLIENT_FRAME_KINDS = frozenset({
    "tool_start", "tool_progress", "tool_output", "ask_user",
    "plan_update", "doc_stream_open", "doc_stream_delta", "doc_update",
    "doc_suggestions", "editor_progress", "ui_control", "email_open",
    "research_progress", "model_request_snapshot", "model_tool_proposal",
    "tool_routing_audit", "tool_resolution_audit", "turn_contract",
    "chat_terminal", "metrics", "stable", "complete",
})


def _safe_turn_control(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, browser-consumed turn-control fields."""
    control: dict[str, Any] = {"reason": kind}
    for key in ("round", "used", "limit", "attempt"):
        if type(data.get(key)) is int and 0 <= data[key] <= 1000000:
            control[key] = data[key]
    message = data.get("message")
    if type(message) is str and message and len(message) <= 200:
        control["message"] = message
    return control


def _safe_web_sources(payload: Any) -> list[dict[str, str]] | None:
    """Filter a provider-derived web source list to bounded public metadata."""
    if not isinstance(payload, list) or len(payload) > _WEB_SOURCE_MAX_ITEMS:
        return None
    sources: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or len(item) > 16:
            return None
        url = item.get("url")
        if type(url) is not str or not _URL_PATTERN.fullmatch(url):
            return None
        filtered = {"url": url}
        for key in ("title", "snippet", "content"):
            if key in item:
                value = item.get(key)
                if type(value) is not str or len(value) > _WEB_SOURCE_MAX_CHARS:
                    return None
                if value:
                    filtered[key] = value
        sources.append(filtered)
    return sources


async def translate_legacy_turn_events(
    frames: AsyncIterator[str | CoreNativeEvent],
    outcome: TurnOutcome,
    *,
    client_rendering: bool = False,
    records: Any = None,
    turn_id: str | None = None,
    turn_owner: str | None = None,
) -> AsyncIterator[TurnEvent]:
    """Translate safe legacy SSE signals and terminate a process-local turn.

    Treat provider text as untrusted. Visible answer text, native tool and
    approval metadata, source-owned model route selections, sanitized usage
    and generic terminal outcomes are the only admitted typed families.
    Tool arguments/results, hidden reasoning, SSE IDs, provider diagnostics
    and unknown payloads never enter typed events.

    With ``records`` and an explicit ``turn_id``, every emitted typed event
    is persisted as an ordered, sanitized durable event and the turn is
    finalized exactly once at its terminal state.

    Closing this iterator explicitly closes the inherited Agent Loop before
    its bridge and contract contextvars are reset.
    """
    if type(client_rendering) is not bool:
        raise ValueError("Invalid trusted client rendering flag")
    if records is not None and (
        type(turn_id) is not str or not turn_id
        or type(turn_owner) is not str or not turn_owner
    ):
        raise ValueError("Durable turn recording requires a turn id and owner")
    if not isinstance(outcome, TurnOutcome):
        raise TypeError("A server-created turn outcome is required")
    if outcome.state != "pending":
        raise ValueError("Turn outcomes cannot be reused")

    def durable(event_value: TurnEvent) -> None:
        """Persist one sanitized typed event; recording never blocks the turn."""
        if records is None:
            return
        try:
            records.append_event(
                turn_id=turn_id, owner=turn_owner,
                sequence=event_value.sequence,
                kind=event_value.type, payload=event_value.payload,
            )
        except Exception:
            # Recording is an audit trail; a failure must not alter the
            # user-visible stream or retry the model.
            logger.warning("Durable turn event recording failed", exc_info=True)

    def durable_terminal(status: str) -> None:
        if records is None:
            return
        try:
            records.finalize_turn(turn_id=turn_id, owner=turn_owner, status=status)
        except Exception:
            logger.warning("Durable turn finalization failed", exc_info=True)

    outcome.state = "running"
    buffer = ""
    pending_cr = False
    deltas: list[str] = []
    final_response: str | None = None
    awaiting_approval: str | None = None

    def event(kind: str, payload: dict[str, Any]) -> TurnEvent:
        outcome.last_sequence += 1
        result = TurnEvent(outcome.last_sequence, kind, payload)
        durable(result)
        return result

    try:
        async with aclosing(frames) as source:
            async for chunk in source:
                if isinstance(chunk, CoreModelRouteEvent):
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before admitted route event")
                    if outcome.state in ("completed", "failed", "awaiting_approval"):
                        continue
                    if (
                        chunk.type != "model.route.selected"
                        or type(chunk.selected_model) is not str
                        or not chunk.selected_model.strip()
                        or len(chunk.selected_model) > 256
                        or type(chunk.answering_model) is not str
                        or not chunk.answering_model.strip()
                        or len(chunk.answering_model) > 256
                        or type(chunk.candidate_index) is not int
                        or chunk.candidate_index <= 0
                        or type(chunk.round) is not int
                        or not 0 < chunk.round <= 100000
                    ):
                        raise ValueError("Invalid admitted model route observation")
                    yield event("model.route.selected", {
                        "selected_model": chunk.selected_model,
                        "answering_model": chunk.answering_model,
                        "candidate_index": chunk.candidate_index,
                        "round": chunk.round,
                    })
                    continue
                if isinstance(chunk, (CoreToolEvent, CoreApprovalEvent)):
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before native Core event")
                    if outcome.state in ("completed", "failed", "awaiting_approval"):
                        continue
                    if isinstance(chunk, CoreToolEvent):
                        if chunk.type not in {
                            "tool.started", "tool.progress", "tool.completed",
                            "tool.failed", "tool.blocked", "tool.awaiting_approval",
                        }:
                            raise ValueError("Invalid native Core tool type")
                        tool = chunk.tool if (
                            type(chunk.tool) is str
                            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", chunk.tool)
                        ) else None
                        yield event(chunk.type, {
                            "tool": tool,
                            "round": chunk.round if type(chunk.round) is int and chunk.round >= 0 else None,
                            "exit_code": chunk.exit_code if type(chunk.exit_code) is int else None,
                        })
                    else:
                        if (
                            chunk.type not in {"approval.requested", "approval.decided"}
                            or type(chunk.approval_id) is not str
                            or re.fullmatch(r"[A-Za-z0-9_-]{43}", chunk.approval_id) is None
                            or (chunk.type == "approval.requested" and chunk.decision is not None)
                            or (chunk.type == "approval.decided" and chunk.decision not in {
                                "approve", "approve_task", "deny",
                            })
                        ):
                            raise ValueError("Invalid native Core approval event")
                        if chunk.type == "approval.requested":
                            awaiting_approval = chunk.approval_id
                        elif awaiting_approval == chunk.approval_id:
                            awaiting_approval = None
                        payload = {"approval_id": chunk.approval_id}
                        if chunk.decision is not None:
                            payload["decision"] = chunk.decision
                        yield event(chunk.type, payload)
                    continue
                if isinstance(chunk, (CoreTextEvent, CoreMetricsEvent, CoreFailureEvent)):
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before native Core event")
                    if outcome.state in ("completed", "failed", "awaiting_approval"):
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
                                "input_tokens", "output_tokens", "total_tokens",
                                "cached_tokens", "reasoning_tokens",
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
                        durable_terminal("failed")
                    continue
                if isinstance(chunk, CoreTerminalEvent):
                    # Core-native completion for admitted, tools-disabled
                    # direct turns: no SSE decode is needed for this terminal.
                    if chunk.type != "turn.completed":
                        raise ValueError("Invalid Agent Core terminal signal")
                    if outcome.state in ("completed", "failed", "awaiting_approval"):
                        continue
                    if buffer.strip() or pending_cr:
                        raise ValueError("Incomplete legacy frame before Core terminal")
                    outcome.content = (
                        final_response if final_response is not None
                        else "".join(deltas)
                    )
                    outcome.state = "awaiting_approval" if awaiting_approval else "completed"
                    yield event("message.final", {"text": outcome.content})
                    yield event(
                        "turn.awaiting_approval" if awaiting_approval else "turn.completed",
                        {"approval_id": awaiting_approval} if awaiting_approval else {},
                    )
                    durable_terminal(
                        "awaiting_approval" if awaiting_approval else "completed",
                    )
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
                    if outcome.state in ("completed", "failed", "awaiting_approval"):
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
                        durable_terminal("failed")
                        continue
                    if raw == "[DONE]":
                        outcome.content = (
                            final_response if final_response is not None
                            else "".join(deltas)
                        )
                        outcome.state = "awaiting_approval" if awaiting_approval else "completed"
                        yield event("message.final", {"text": outcome.content})
                        yield event(
                            "turn.awaiting_approval" if awaiting_approval else "turn.completed",
                            {"approval_id": awaiting_approval} if awaiting_approval else {},
                        )
                        durable_terminal(
                            "awaiting_approval" if awaiting_approval else "completed",
                        )
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # Do not reflect malformed provider data.
                        continue
                    if not isinstance(data, dict):
                        continue
                    kind = data.get("type")
                    if client_rendering and kind in _CLIENT_FRAME_KINDS:
                        # The trusted client adapter owns this family's exact
                        # browser rendering; the frame is already parsed JSON
                        # inside the safety-limited legacy envelope.
                        yield event("client.frame", data)
                        continue
                    if kind == "agent_terminal":
                        metadata = data.get("data")
                        status = _safe_status(metadata)
                        outcome.failure_code = "agent_terminal"
                        outcome.state = "failed"
                        yield event("turn.failed", {
                            "code": "agent_terminal", "status": status,
                        })
                        durable_terminal("failed")
                    elif kind == "final_response" and isinstance(data.get("content"), str):
                        final_response = data["content"]
                    elif kind == "usage":
                        yield event("usage", _safe_metrics(data.get("data")))
                    elif kind == "agent_step":
                        # Round progress: one bounded integer, no other fields.
                        round_value = data.get("round")
                        if type(round_value) is int and 0 < round_value <= 100000:
                            yield event("agent.step", {"round": round_value})
                    elif kind in (
                        "rounds_exhausted", "budget_exceeded",
                        "loop_breaker_triggered", "intent_nudge_exhausted",
                    ):
                        # Turn-control annotations keep only the bounded
                        # browser-consumed fields, never raw diagnostics.
                        yield event("turn.control", _safe_turn_control(kind, data))
                    elif kind == "web_sources":
                        sources = _safe_web_sources(data.get("data"))
                        if sources is not None:
                            yield event("sources.web", {"sources": sources})
                    elif kind in (None, "delta") and (
                        data.get("thinking") is True
                        or data.get("reasoning") is True
                        or data.get("channel") in ("analysis", "thinking", "reasoning")
                        or isinstance(data.get("reasoning_content"), str)
                    ):
                        # Hidden reasoning is a display-only channel; the
                        # browser wraps it in a collapsible thinking block and
                        # the persistence layer never accumulates it as answer
                        # text.
                        value = (
                            data.get("reasoning_content")
                            if isinstance(data.get("reasoning_content"), str)
                            else data.get("delta")
                        )
                        if isinstance(value, str) and value:
                            yield event("thinking.delta", {"text": value})
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
            durable_terminal("failed")
            yield event("turn.failed", {"code": "incomplete_stream", "status": None})
    except (asyncio.CancelledError, GeneratorExit):
        if outcome.state == "running":
            outcome.state = "cancelled"
            outcome.failure_code = None
            durable_terminal("cancelled")
        raise
    except BaseException:
        if outcome.state == "running":
            outcome.state = "failed"
            outcome.failure_code = "stream_exception"
            durable_terminal("failed")
        raise
