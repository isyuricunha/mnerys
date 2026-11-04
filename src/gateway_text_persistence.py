"""Opt-in conversation persistence for admitted direct plain-text turns.

The shared context ports write user input. This wrapper reuses the existing
owner-admitted assistant and interrupted-response writers. Terminal
acknowledgements follow successful writes; no atomic two-message transaction,
durable receipt, cross-run idempotence or Web cutover is claimed.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied
from src.gateway_assistant_persistence import (
    save_admitted_assistant_response,
    save_admitted_interrupted_assistant_response,
)
from src.gateway_turn_events import TurnEvent, TurnOutcome


@dataclass(frozen=True)
class TrustedTextPersistence:
    """Server-selected legacy writers; never deserialize client-provided ones."""

    session_manager: Any
    saver: Callable[..., Any]
    interrupted_writer: Callable[..., Any] | None = None


def require_trusted_text_persistence(value: Any) -> TrustedTextPersistence:
    if (
        not isinstance(value, TrustedTextPersistence)
        or value.session_manager is None
        or not callable(value.saver)
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
        # Mark before the call: a failing writer may already have committed
        # its side effect. Never repeat an ambiguous append.
        partial_attempted = True
        save_admitted_interrupted_assistant_response(
            admission, session, writer.session_manager,
            session.id, content,
            {"stopped": True, "interruption_reason": reason},
            context_owner=context_owner,
            writer=writer.interrupted_writer,
        )

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
                        save_admitted_assistant_response(
                            admission, session, writer.session_manager,
                            session.id, outcome.content, last_metrics,
                            context_owner=context_owner, saver=writer.saver,
                        )
                    except Exception:
                        terminal_seen = True
                        yield persistence_failure()
                        return
                    terminal_seen = True
                    if buffered_final is not None:
                        last_visible_sequence = buffered_final.sequence
                        yield buffered_final
                    last_visible_sequence = event.sequence
                    yield event
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
