"""Opt-in persistence for admitted, completed direct text turns.

This reuses the Web assistant writer after the shared context ports append the
user message. Writes are not atomic across both messages, nor durable turn
receipts. The direct profile excludes tools and approval controls until the
full session transaction and continuation contracts are implemented.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied
from src.gateway_assistant_persistence import save_admitted_assistant_response
from src.gateway_turn_events import TurnEvent, TurnOutcome


@dataclass(frozen=True)
class TrustedTextPersistence:
    """Trusted server-owned legacy saver; never accept from a client payload."""

    session_manager: Any
    saver: Callable[..., Any]


def require_trusted_text_persistence(value: Any) -> TrustedTextPersistence:
    if (
        not isinstance(value, TrustedTextPersistence)
        or value.session_manager is None
        or not callable(value.saver)
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
    """Persist before acknowledging completion, never for unfinished turns.

    The user input is appended by the shared context algorithm before agent
    execution. A writer failure is reported as a sanitized terminal failure;
    it does not claim a successful persisted assistant message. No retry or
    cross-run idempotence is implied by this in-process boundary.
    """
    writer = require_trusted_text_persistence(persistence)
    last_metrics: dict[str, int] | None = None
    saved = False
    async with aclosing(events) as source:
        async for event in source:
            if event.type == "metrics":
                last_metrics = {
                    key: value for key, value in event.payload.items()
                    if key in {"input_tokens", "output_tokens", "total_tokens"}
                    and type(value) is int and value >= 0
                }
            if event.type == "turn.completed":
                if saved:
                    continue
                # The typed adapter has already established the terminal
                # outcome and filtered user-visible response.
                if outcome.state != "completed":
                    raise RuntimeError("A completed turn outcome is required")
                try:
                    save_admitted_assistant_response(
                        admission, session, writer.session_manager,
                        session.id, outcome.content, last_metrics,
                        context_owner=context_owner, saver=writer.saver,
                    )
                except Exception:
                    outcome.state = "failed"
                    outcome.failure_code = "persistence_error"
                    outcome.last_sequence += 1
                    yield TurnEvent(
                        outcome.last_sequence, "turn.failed",
                        {"code": "persistence_error", "status": None},
                    )
                    return
                saved = True
            yield event
