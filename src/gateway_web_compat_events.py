"""Read-only M2.1 text-event serialization for a future Web adapter.

The production Web route still emits its inherited SSE. This module
serializes *already filtered* Gateway turn events to the existing browser's
plain-chat SSE subset for no-HTTP parity tests. It performs no admission,
tool execution, message saves, background work or network I/O.

The restricted manage_notes preview also accepts original, store-verified
review cards and filtered status metadata. Detached replay reconstructs
the exact typed run prefix before filtering acknowledged SSE cursor frames.
Neither mode authorizes a tool or enables production Web routing.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
import json
import re

from src.gateway_turn_events import TurnEvent, TypedRunEvent
from src.gateway_web_approval_cards import SealedReviewCard


_MAX_TEXT_CHARS = 4 * 1024 * 1024
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}")
_SAVED_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_REVIEW_CURSOR_RE = re.compile(r"[0-9a-f]{32}:(?:0|[1-9][0-9]*)")
_TYPED_HEARTBEAT_RE = re.compile(r": heartbeat [1-9][0-9]*\n\n")
_SAFE_METRICS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_SAFE_FAILURE_CODES = frozenset({
    "provider_error", "agent_terminal", "incomplete_stream",
    "stream_exception", "persistence_error",
})
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{43}")
# A named, audited initial status surface. These are timeline annotations,
# not a route to tool execution or UI-side effects.
_TOOL_STATUS_PREVIEW = frozenset({"manage_notes"})


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


def _tool_status_frame(
    event: TurnEvent,
    *,
    permitted: frozenset[str],
    active: tuple[str, int | None] | None,
) -> tuple[str, tuple[str, int | None] | None]:
    """Browser-compatible status without arguments, results or UI actions."""
    kind, data = event.type, event.payload
    if type(data) is not dict or set(data) != {"tool", "round", "exit_code"}:
        raise ValueError("Invalid native tool status metadata")
    tool = data["tool"]
    round_number = data["round"]
    exit_code = data["exit_code"]
    if (
        type(tool) is not str or not _TOOL_NAME.fullmatch(tool)
        or tool not in permitted or tool not in _TOOL_STATUS_PREVIEW
        or (round_number is not None and
            (type(round_number) is not int or not 0 <= round_number <= 100000))
        or (exit_code is not None and
            (type(exit_code) is not int or not -255 <= exit_code <= 255))
    ):
        raise ValueError("Tool is not admitted for the preview timeline")
    if kind == "tool.awaiting_approval":
        # An opaque ID cannot become an actionable Web review card.
        raise ValueError("Unsupported tool status for Web approval rendering")
    identity = (tool, round_number)
    if kind == "tool.started":
        if active is not None or exit_code is not None:
            raise ValueError("Overlapping or invalid tool start")
        return _data_frame({"type": "tool_start", "tool": tool, "command": ""}), identity
    if active != identity:
        raise ValueError("Tool status does not match its started action")
    if kind == "tool.progress":
        if exit_code is not None:
            raise ValueError("Invalid progress exit code")
        return _data_frame({
            "type": "tool_progress", "tool": tool,
            "message": "Tool is running",
        }), active
    if kind not in {"tool.completed", "tool.failed", "tool.blocked"}:
        raise ValueError("Unsupported tool status type")
    if kind == "tool.completed":
        if exit_code not in {None, 0}:
            raise ValueError("Completed tool has a failure exit code")
        status = 0
    elif kind == "tool.failed":
        if exit_code == 0:
            raise ValueError("Failed tool has a success exit code")
        status = exit_code if exit_code is not None else 1
    else:
        # Never show an unexecuted/blocked tool as a successful effect.
        status = 1
    return _data_frame({
        "type": "tool_output", "tool": tool,
        "command": "", "output": "", "exit_code": status,
    }), None


def _approval_decision_frame(event: TurnEvent) -> str:
    """Only a consumed Core decision may remove an existing Web review card.

    This produces no approval button and cannot execute or grant anything.
    """
    data = event.payload
    if (
        type(data) is not dict
        or set(data) != {"approval_id", "decision"}
        or type(data["approval_id"]) is not str
        or _APPROVAL_ID.fullmatch(data["approval_id"]) is None
        or type(data["decision"]) is not str
        or data["decision"] not in {"approve", "approve_task", "deny"}
    ):
        raise ValueError("Invalid consumed Core approval decision")
    return _data_frame({
        "type": "tool_approval_resolved", "decision": data["decision"],
    })


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
    tool_status_preview: frozenset[str] = frozenset(),
    approval_card_resolver: Callable[[str], SealedReviewCard] | None = None,
    replay_review: bool = False,
    after_cursor: str | None = None,
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
        or type(replay_review) is not bool
        or (replay_review and (
            not replay or prior_saved_receipt or approval_card_resolver is None
        ))
        or (after_cursor is not None and (
            not replay_review or type(after_cursor) is not str
            or _REVIEW_CURSOR_RE.fullmatch(after_cursor) is None
        ))
        or type(tool_status_preview) is not frozenset
        or not tool_status_preview <= _TOOL_STATUS_PREVIEW
        or (approval_card_resolver is not None and (
            not callable(approval_card_resolver)
            or not tool_status_preview
            or (replay and not replay_review)
        ))
    ):
        raise TypeError("Invalid Web compatibility serialization options")
    previous = 0
    mode: bool | None = None
    run_id: str | None = None
    receipt_seen = prior_saved_receipt
    terminal_seen = False
    active_tool: tuple[str, int | None] | None = None
    pending_review: SealedReviewCard | None = None
    awaiting_tool_notice = False
    pending_terminal = False
    pending_terminal_cursor: str | None = None
    requested_run_id, _, requested_seq = (
        after_cursor.partition(":") if after_cursor is not None else ("", "", "")
    )
    requested_index = int(requested_seq) if requested_seq else -1

    def emit_cursor(cursor: str | None) -> bool:
        if after_cursor is None:
            return True
        if cursor is None or cursor.split(":", 1)[0] != requested_run_id:
            raise ValueError("Review cursor does not match the exact replayed run")
        return int(cursor.rsplit(":", 1)[1]) > requested_index

    async with aclosing(events) as source:
        async for item in source:
            # The original detached subscription injects only SSE comments
            # while a provider is idle. They carry no event ID or authority.
            if type(item) is str and replay and _TYPED_HEARTBEAT_RE.fullmatch(item):
                yield item
                continue
            event, cursor = _event_identity(item)
            if pending_terminal:
                raise ValueError("Unexpected event after awaiting-review terminal")
            has_cursor = cursor is not None
            if has_cursor != replay:
                raise ValueError("Web compatibility event representation mismatch")
            if mode is not None and mode != has_cursor:
                raise ValueError("Mixed typed replay and direct events")
            mode = has_cursor
            if has_cursor:
                current_run = item.run_id
                if after_cursor is not None and current_run != requested_run_id:
                    raise ValueError("Review replay switched its run identity")
                if run_id is not None and run_id != current_run:
                    raise ValueError("Typed replay switched run identity")
                run_id = current_run
            if previous and event.sequence != previous + 1:
                raise ValueError("Out-of-order typed Web events")
            if not previous and not has_cursor and event.sequence != 1:
                raise ValueError("Direct typed stream started out of order")
            previous = event.sequence
            if event.type.startswith("tool.") or event.type.startswith("approval."):
                if not tool_status_preview:
                    raise ValueError("Tool timeline was not admitted")
                if event.type == "approval.requested" and approval_card_resolver is not None:
                    if (
                        pending_review is not None or active_tool is not None
                        or type(event.payload) is not dict
                        or set(event.payload) != {"approval_id"}
                        or type(event.payload["approval_id"]) is not str
                    ):
                        raise ValueError("Invalid pending review event order")
                    review = approval_card_resolver(event.payload["approval_id"])
                    if type(review) is not SealedReviewCard or (
                        review.approval_id != event.payload["approval_id"]
                    ):
                        raise ValueError("Pending review did not match the sealed store")
                    pending_review = review
                    continue
                if event.type == "tool.awaiting_approval" and pending_review is not None:
                    if (
                        awaiting_tool_notice or active_tool is not None
                        or type(event.payload) is not dict
                        or set(event.payload) != {"tool", "round", "exit_code"}
                        or event.payload.get("tool") != "manage_notes"
                        or event.payload.get("exit_code") is not None
                    ):
                        raise ValueError("Invalid pending tool review status")
                    # Do not pretend a gated tool started or completed. The
                    # original Web review card, if still valid, is shown only
                    # after the turn's awaiting-approval terminal handshake.
                    awaiting_tool_notice = True
                    continue
                if pending_review is not None:
                    raise ValueError("Unexpected action while review is pending")
                if event.type.startswith("tool."):
                    frame, active_tool = _tool_status_frame(
                        event, permitted=tool_status_preview, active=active_tool,
                    )
                elif event.type == "approval.decided" and active_tool is None:
                    frame = _approval_decision_frame(event)
                else:
                    raise ValueError("Pending or unresolved approval cannot be rendered")
                if cursor is not None:
                    frame = "id: " + cursor + "\n" + frame
                if emit_cursor(cursor):
                    yield frame
                continue
            if event.type == "turn.awaiting_approval":
                if (
                    approval_card_resolver is None
                    or pending_review is None or not awaiting_tool_notice
                    or active_tool is not None
                    or type(event.payload) is not dict
                    or event.payload != {"approval_id": pending_review.approval_id}
                ):
                    raise ValueError("Unverified pending Web approval terminal")
                # Await the original stream's normal completion and cleanup
                # before exposing any actionable review card or [DONE].
                pending_terminal = True
                pending_terminal_cursor = cursor
                continue
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
            if event.type == "turn.completed" and pending_review is not None:
                raise ValueError("Review cannot be acknowledged as completed work")
            if event.type in {"message.final", "turn.completed", "turn.failed"} and active_tool is not None:
                raise ValueError("Tool timeline ended without a terminal tool status")
            frame, terminal = serialize_plain_text_turn_event(event)
            if cursor is not None:
                frame = "id: " + cursor + "\n" + frame
            if emit_cursor(cursor):
                yield frame
            if terminal:
                terminal_seen = True
                return
    if pending_terminal:
        if pending_review is None or approval_card_resolver is None:
            raise ValueError("Unverified pending Web review")
        current = approval_card_resolver(pending_review.approval_id)
        if (
            type(current) is not SealedReviewCard
            or current.approval_id != pending_review.approval_id
            or current.digest != pending_review.digest
            or current.payload != pending_review.payload
        ):
            raise ValueError("Pending review changed before display")
        if emit_cursor(pending_terminal_cursor):
            frame = _data_frame({"type": "ask_user", "data": current.payload})
            if pending_terminal_cursor is not None:
                frame = "id: " + pending_terminal_cursor + "\n" + frame
            yield frame
            yield "data: [DONE]\n\n"
        return
    # Replayed subscriptions can intentionally begin *after* the previously
    # acknowledged terminal event. They have nothing further to send.
    if not terminal_seen and not replay:
        raise ValueError("Direct typed stream ended without a terminal event")
