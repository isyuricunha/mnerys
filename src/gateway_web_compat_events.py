"""Read-only M2.1 text-event serialization for a future Web adapter.

The production Web route still emits its inherited SSE. This module
serializes *already filtered* Gateway turn events to the existing browser's
plain-chat SSE subset for no-HTTP parity tests. It performs no admission,
tool execution, message saves, background work or network I/O.

Do not use it for tool/approval/editor turns until their full Web payloads
and authorization contracts have accepted parity tests.
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
import json
import re

from src.gateway_turn_events import TurnEvent, TypedRunEvent


_MAX_TEXT_CHARS = 4 * 1024 * 1024
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}")
_SAVED_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_SAFE_METRICS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_SAFE_FAILURE_CODES = frozenset({
    "provider_error", "agent_terminal", "incomplete_stream",
    "stream_exception", "persistence_error",
})


def _data_frame(payload: dict) -> str:
    return "data: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ) + "\n\n"


def _safe_failure_frame(payload: object) -> str:
    if type(payload) is not dict or payload.get("code") not in _SAFE_FAILURE_CODES:
        raise ValueError("Unsupported typed failure metadata")
    status = payload.get("status")
    if type(status) is not int or not 400 <= status <= 599:
        status = 500
    # Do not surface provider exceptions, untrusted prompts or write errors.
    return "event: error\ndata: " + json.dumps({
        "error": "The request could not be completed.", "status": status,
    }, separators=(",", ":")) + "\n\n"


def serialize_plain_text_turn_event(event: TurnEvent) -> tuple[str, bool]:
    """Map only browser-supported, permissionless fields to legacy SSE.

    The result's second value is whether the event is terminal. A terminal
    success requires the upstream composer to have emitted turn.completed
    *after* its admitted assistant writer confirmed a fresh receipt.
    """
    if (
        type(event) is not TurnEvent or type(event.sequence) is not int
        or event.sequence <= 0 or type(event.type) is not str
        or type(event.payload) is not dict
    ):
        raise ValueError("Invalid typed Web compatibility event")
    kind, data = event.type, event.payload
    if kind in {"message.delta", "message.final"}:
        text = data.get("text")
        if type(text) is not str or len(text) > _MAX_TEXT_CHARS:
            raise ValueError("Invalid visible text in typed event")
        if kind == "message.delta":
            return _data_frame({"delta": text}), False
        return _data_frame({"type": "final_response", "content": text}), False
    if kind == "message.saved":
        message_id = data.get("id")
        if type(message_id) is not str or not _SAVED_ID_RE.fullmatch(message_id):
            raise ValueError("Invalid admitted assistant message receipt")
        return _data_frame({"type": "message_saved", "id": message_id}), False
    if kind in {"metrics", "usage"}:
        if any(
            key not in _SAFE_METRICS or type(value) is not int or value < 0
            for key, value in data.items()
        ):
            raise ValueError("Invalid sanitized usage metadata")
        # The existing Web browser renders 'metrics'; 'usage' is its
        # lower-level provider frame. Keep their names distinct.
        return _data_frame({"type": kind, "data": data}), False
    if kind == "model.route.selected":
        selected, answering = data.get("selected_model"), data.get("answering_model")
        index, round_number = data.get("candidate_index"), data.get("round")
        if (
            type(selected) is not str or not _MODEL_RE.fullmatch(selected)
            or type(answering) is not str or not _MODEL_RE.fullmatch(answering)
            or type(index) is not int or index <= 0
            or type(round_number) is not int or round_number <= 0
        ):
            raise ValueError("Invalid admitted fallback model")
        # The route choice comes from the Core selector, not the provider.
        return _data_frame({
            "type": "fallback", "selected_model": selected,
            "answered_by": answering, "candidate_index": index,
            "round": round_number,
        }), False
    if kind == "turn.completed":
        if data:
            raise ValueError("Unexpected completion metadata")
        return "data: [DONE]\n\n", True
    if kind == "turn.failed":
        return _safe_failure_frame(data), True
    # Tool/approval/model-side effect shapes are not the plain-text Web
    # contract. Fail closed rather than inventing an actionable browser card.
    raise ValueError("Typed event has no approved plain-text Web representation")


def _event_identity(
    item: TurnEvent | TypedRunEvent,
) -> tuple[TurnEvent, str | None]:
    if type(item) is TurnEvent:
        return item, None
    if type(item) is not TypedRunEvent:
        raise ValueError("Invalid typed replay event")
    run_id = item.run_id
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("Invalid typed replay run identity")
    if type(item.cursor) is not str:
        raise ValueError("Invalid typed replay cursor")
    head, sep, seq = item.cursor.partition(":")
    if (
        not sep or head != run_id or not seq.isascii()
        or not seq.isdecimal() or str(int(seq)) != seq
        or type(item.event) is not TurnEvent
        or type(item.event.sequence) is not int
        or item.event.sequence != int(seq) + 1
    ):
        raise ValueError("Typed replay cursor does not match its event")
    return item.event, item.cursor


async def serialize_plain_text_web_sse(
    events: AsyncIterator[TurnEvent | TypedRunEvent],
    *,
    require_saved_receipt: bool = True,
    replay: bool = False,
    prior_saved_receipt: bool = False,
) -> AsyncIterator[str]:
    """Serialize an approved text-only stream; never authorize a tool turn.

    The optional event IDs are produced by the original process-local
    agent_runs registry and retain its exact run-scoped replay cursor.
    A resumed stream may begin at a sequence greater than one.
    """
    if (
        type(require_saved_receipt) is not bool
        or type(replay) is not bool
        or type(prior_saved_receipt) is not bool
        or (prior_saved_receipt and not replay)
    ):
        raise TypeError("Invalid Web compatibility serialization options")
    previous = 0
    mode: bool | None = None
    run_id: str | None = None
    receipt_seen = prior_saved_receipt
    terminal_seen = False
    async with aclosing(events) as source:
        async for item in source:
            event, cursor = _event_identity(item)
            has_cursor = cursor is not None
            if has_cursor != replay:
                raise ValueError("Web compatibility event representation mismatch")
            if mode is not None and mode != has_cursor:
                raise ValueError("Mixed typed replay and direct events")
            mode = has_cursor
            if has_cursor:
                current_run = item.run_id
                if run_id is not None and run_id != current_run:
                    raise ValueError("Typed replay switched run identity")
                run_id = current_run
            if previous and event.sequence != previous + 1:
                raise ValueError("Out-of-order typed Web events")
            if not previous and not has_cursor and event.sequence != 1:
                raise ValueError("Direct typed stream started out of order")
            previous = event.sequence
            if event.type == "message.saved":
                if receipt_seen:
                    raise ValueError("Duplicate saved message receipt")
                receipt_seen = True
            if (
                event.type == "message.final"
                and require_saved_receipt and not receipt_seen
            ):
                raise ValueError("Web final response has no confirmed assistant receipt")
            if (
                event.type == "turn.completed"
                and require_saved_receipt and not receipt_seen
            ):
                raise ValueError("Completed Web turn has no confirmed assistant receipt")
            # A replay may omit an already acknowledged receipt only when
            # the original run registry proved it preceded this cursor.
            frame, terminal = serialize_plain_text_turn_event(event)
            if cursor is not None:
                frame = "id: " + cursor + "\n" + frame
            yield frame
            if terminal:
                terminal_seen = True
                return
    # Replayed subscriptions can intentionally begin *after* the previously
    # acknowledged terminal event. They have nothing further to send.
    if not terminal_seen and not replay:
        raise ValueError("Direct typed stream ended without a terminal event")
