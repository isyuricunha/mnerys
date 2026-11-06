"""Opt-in conversation persistence for admitted text and restricted native turns.

The shared context ports write user input. This wrapper reuses the existing
owner-admitted assistant and interrupted-response writers. Terminal
acknowledgements follow successful writes; no atomic two-message transaction,
durable receipt, cross-run idempotence or Web cutover is claimed.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
import logging
import re
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied
from src.gateway_assistant_persistence import (
    save_admitted_assistant_response,
    save_admitted_interrupted_assistant_response,
)
from src.gateway_turn_events import TurnEvent, TurnOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrustedTextPersistence:
    """Server-selected legacy writers; never deserialize client-provided ones."""

    session_manager: Any
    saver: Callable[..., Any]
    interrupted_writer: Callable[..., Any] | None = None
    # Only the trusted text-to-Web compatibility adapter selects this.
    # The receipt remains the inherited writer's post-commit message ID.
    emit_message_saved_events: bool = False
    # Trusted optional Web compatibility hooks only; both execute *after*
    # the corresponding existing writer has confirmed its database receipt.
    # No new scheduler or persistence implementation is introduced.
    after_user_saved: Callable[[], None] | None = None
    after_assistant_saved: Callable[[str, dict[str, Any] | None], None] | None = None
    # Only an admitted trusted notes adapter may register the verified review
    # *before* its actionable card leaves the original typed stream.
    after_review_prepared: Callable[[str], None] | None = None
    # A one-use SQL fence is reserved immediately before the original Core
    # consumes and dispatches the exact first action. An inconclusive reserve
    # must never be automatically retried after a process crash.
    before_approval_consume: Callable[[Any, str, str], None] | None = None


class LegacyWriteNotConfirmed(RuntimeError):
    """A trusted legacy writer did not produce a fresh database receipt."""


def snapshot_session_receipts(session: Any) -> tuple[list, int, frozenset[str]]:
    """Observe the actual managed Session transcript before one legacy append."""
    history = getattr(session, "history", None)
    if not isinstance(history, list):
        raise AdmissionDenied(403, "A managed session history is required")
    existing_ids: set[str] = set()
    for message in history:
        metadata = getattr(message, "metadata", None)
        if isinstance(metadata, dict):
            receipt = metadata.get("_db_id")
            if type(receipt) is str and receipt:
                existing_ids.add(receipt)
    return history, len(history), frozenset(existing_ids)


def require_new_session_receipt(
    session: Any,
    cursor: tuple[list, int, frozenset[str]],
    role: str,
) -> str:
    """Reject silent failures of the inherited SessionManager append.

    The legacy manager sets the fresh database ID in message metadata only
    after commit. This is a process-local compatibility check, not a durable
    generation fence or verification against an independent database read.
    """
    history, previous_length, old_receipts = cursor
    if (
        getattr(session, "history", None) is not history
        or len(history) != previous_length + 1
    ):
        raise LegacyWriteNotConfirmed("Legacy message append was not committed")
    message = history[-1]
    metadata = getattr(message, "metadata", None)
    receipt = metadata.get("_db_id") if isinstance(metadata, dict) else None
    if (
        getattr(message, "role", None) != role
        or type(receipt) is not str
        or not receipt.strip()
        or receipt in old_receipts
    ):
        raise LegacyWriteNotConfirmed("Legacy message append lacks a new database receipt")
    return receipt


def require_persisted_native_review_origin(session: Any, pending: Any) -> str:
    """Require the pending note review to follow its confirmed original user.

    This is deliberately process-local, using the existing legacy receipt.
    It does not claim a durable run/message fence or authorize the tool.
    """
    history = getattr(session, "history", None)
    request_text = getattr(pending, "request_text", None)
    if (
        not isinstance(history, list) or not history
        or type(request_text) is not str or not request_text.strip()
    ):
        raise AdmissionDenied(409, "Persisted native review has no original message")
    original = history[-1]
    metadata = getattr(original, "metadata", None)
    receipt = metadata.get("_db_id") if isinstance(metadata, dict) else None
    if (
        getattr(original, "role", None) != "user"
        or getattr(original, "content", None) != request_text
        or type(receipt) is not str or not receipt.strip()
        or any(
            isinstance(getattr(item, "metadata", None), dict)
            and item.metadata.get("_db_id") == receipt
            for item in history[:-1]
        )
    ):
        raise AdmissionDenied(409, "Persisted native review origin is stale or unconfirmed")
    return receipt


def require_trusted_text_persistence(value: Any) -> TrustedTextPersistence:
    if (
        not isinstance(value, TrustedTextPersistence)
        or value.session_manager is None
        or not callable(value.saver)
        or type(value.emit_message_saved_events) is not bool
        or (value.after_user_saved is not None and not callable(value.after_user_saved))
        or (value.after_assistant_saved is not None and not callable(value.after_assistant_saved))
        or (value.after_review_prepared is not None and not callable(value.after_review_prepared))
        or (value.before_approval_consume is not None and not callable(value.before_approval_consume))
        or (
            value.interrupted_writer is not None
            and not callable(value.interrupted_writer)
        )
    ):
        raise AdmissionDenied(403, "Invalid trusted text persistence writer")
    return value


async def persist_completed_text_events(
    events: AsyncIterator[TurnEvent],
    outcome: TurnOutcome,
    *,
    admission: AdmittedSession,
    session: Any,
    context_owner: str | None,
    persistence: TrustedTextPersistence,
) -> AsyncIterator[TurnEvent]:
    """Wrap one stream without treating an uncommitted answer as completed.

    The optional interrupted writer saves only visible partial answer deltas
    when the stream fails or is closed. It is a compatibility save, not a
    guaranteed atomic receipt: an ambiguous writer error is never retried
    automatically.
    """
    writer = require_trusted_text_persistence(persistence)
    last_metrics: dict[str, int] | None = None
    # The only source of actual fallback attribution is a native Core model
    # route event; provider-authored SSE or HTTP fields cannot mint one.
    selected_route: dict[str, str] | None = None
    visible_deltas: list[str] = []
    buffered_final: TurnEvent | None = None
    final_attempted = False
    partial_attempted = False
    terminal_seen = False
    last_visible_sequence = 0

    def save_partial(reason: str) -> None:
        nonlocal partial_attempted
        if (
            final_attempted or partial_attempted
            or writer.interrupted_writer is None
        ):
            return
        content = "".join(visible_deltas)
        if not content.strip():
            return
        cursor = snapshot_session_receipts(session)
        # Mark before the call: a failing writer may already have committed
        # its side effect. Never repeat an ambiguous append.
        partial_attempted = True
        save_admitted_interrupted_assistant_response(
            admission, session, writer.session_manager,
            session.id, content,
            {
                "stopped": True, "interruption_reason": reason,
                **(selected_route or {}),
            },
            context_owner=context_owner,
            writer=writer.interrupted_writer,
        )
        require_new_session_receipt(session, cursor, "assistant")

    def persistence_failure() -> TurnEvent:
        nonlocal last_visible_sequence
        outcome.state = "failed"
        outcome.failure_code = "persistence_error"
        last_visible_sequence += 1
        outcome.last_sequence = last_visible_sequence
        return TurnEvent(
            last_visible_sequence, "turn.failed",
            {"code": "persistence_error", "status": None},
        )

    try:
        async with aclosing(events) as source:
            async for event in source:
                if event.type == "model.route.selected":
                    selected = event.payload.get("selected_model")
                    answered = event.payload.get("answering_model")
                    if (
                        type(selected) is not str or selected != session.model
                        or type(answered) is not str
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}", answered) is None
                        or selected_route is not None
                    ):
                        raise AdmissionDenied(409, "Invalid admitted model route attribution")
                    selected_route = {
                        "requested_model": selected,
                        "model": answered,
                    }
                if event.type == "message.delta":
                    visible_deltas.append(event.payload["text"])
                elif event.type == "metrics":
                    last_metrics = {
                        key: value for key, value in event.payload.items()
                        if key in {"input_tokens", "output_tokens", "total_tokens"}
                        and type(value) is int and value >= 0
                    }
                elif event.type == "message.final":
                    # A final frame is not an acknowledgement of persistence.
                    # Delay it until the existing assistant saver succeeds.
                    buffered_final = event
                    continue
                elif event.type == "turn.completed":
                    if final_attempted:
                        continue
                    if outcome.state != "completed":
                        raise RuntimeError("A completed turn outcome is required")
                    # A writer may have committed before raising: do not issue
                    # a second save or fall back to an interrupted append.
                    final_attempted = True
                    try:
                        cursor = snapshot_session_receipts(session)
                        save_metadata = {
                            **(last_metrics or {}),
                            **(selected_route or {}),
                        } or None
                        save_admitted_assistant_response(
                            admission, session, writer.session_manager,
                            session.id, outcome.content, save_metadata,
                            context_owner=context_owner, saver=writer.saver,
                        )
                        saved_message_id = require_new_session_receipt(
                            session, cursor, "assistant",
                        )
                        if writer.emit_message_saved_events and (
                            len(saved_message_id) > 128
                            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", saved_message_id) is None
                        ):
                            raise LegacyWriteNotConfirmed("Unsupported assistant receipt")
                    except Exception:
                        terminal_seen = True
                        yield persistence_failure()
                        return
                    # Do not start optional post-response work while the
                    # original LLM/Agent async generator still owns resources.
                    # The terminal was buffered by the typed adapter, so
                    # closing here cannot drop any further user-visible reply.
                    try:
                        await source.aclose()
                    except Exception:
                        logger.warning(
                            "Admitted Agent cleanup failed after confirmed save"
                        )
                    # This optional Web continuation is downstream of the
                    # original successful save *and* fresh receipt check.
                    # Its failure cannot invalidate an already-committed
                    # assistant message or trigger a duplicate writer retry.
                    if writer.after_assistant_saved is not None:
                        try:
                            writer.after_assistant_saved(outcome.content, save_metadata)
                        except Exception:
                            logger.warning(
                                "Admitted post-response dispatch failed after confirmed save"
                            )
                    terminal_seen = True
                    if writer.emit_message_saved_events:
                        # Publish the confirmed receipt before the canonical
                        # final text; Web compatibility must never display
                        # an uncommitted final answer as saved.
                        last_visible_sequence = (
                            buffered_final.sequence
                            if buffered_final is not None else event.sequence
                        )
                        yield TurnEvent(
                            last_visible_sequence, "message.saved",
                            {"id": saved_message_id},
                        )
                        if buffered_final is not None:
                            last_visible_sequence += 1
                            yield TurnEvent(
                                last_visible_sequence,
                                buffered_final.type, buffered_final.payload,
                            )
                        last_visible_sequence += 1
                        outcome.last_sequence = last_visible_sequence
                        yield TurnEvent(
                            last_visible_sequence, event.type, event.payload,
                        )
                    else:
                        if buffered_final is not None:
                            last_visible_sequence = buffered_final.sequence
                            yield buffered_final
                        last_visible_sequence = event.sequence
                        yield event
                    return
                elif event.type == "turn.awaiting_approval":
                    # The optional durable fence is written by the original
                    # detached run, not by a browser subscriber. A disconnected
                    # client cannot skip registration, and a failed journal
                    # write cannot display an actionable approval card.
                    if writer.after_review_prepared is not None:
                        review_id = event.payload.get("approval_id")
                        if type(review_id) is not str or not review_id:
                            terminal_seen = True
                            yield persistence_failure()
                            return
                        try:
                            writer.after_review_prepared(review_id)
                        except Exception:
                            terminal_seen = True
                            yield persistence_failure()
                            return
                    # A sealed pending review is not a completed answer.
                    # Drop the unsaved final text and do not misclassify this
                    # orderly pause as an interrupted assistant write.
                    # Keep the process-local typed sequence gap-free.
                    terminal_seen = True
                    last_visible_sequence += 1
                    outcome.last_sequence = last_visible_sequence
                    yield TurnEvent(
                        last_visible_sequence, event.type, event.payload,
                    )
                    return
                elif event.type == "turn.failed":
                    terminal_seen = True
                    # Only sanitized local failure categories reach metadata.
                    reason = outcome.failure_code
                    if reason not in {
                        "provider_error", "agent_terminal",
                        "incomplete_stream", "stream_exception",
                    }:
                        reason = "stream_failed"
                    try:
                        save_partial(reason)
                    except Exception:
                        yield persistence_failure()
                        return
                    last_visible_sequence = event.sequence
                    yield event
                    return
                last_visible_sequence = event.sequence
                yield event
    except LegacyWriteNotConfirmed:
        # A failed user append happens before the model can run. Unlike a
        # provider exception, the inherited writer failure has a known,
        # sanitized persistence terminal; do not expose its details.
        terminal_seen = True
        yield persistence_failure()
    finally:
        # Closing/cancelling an async generator cannot yield another event.
        # Reuse the admitted interrupted writer synchronously, preserving the
        # original close/cancellation/exception even if that writer fails.
        if not terminal_seen:
            try:
                reason = (
                    "stream_exception" if outcome.state == "failed"
                    else "cancelled"
                )
                save_partial(reason)
            except Exception:
                # There is no live consumer to acknowledge a write error.
                # Preserve the underlying cancellation or runtime exception.
                pass
