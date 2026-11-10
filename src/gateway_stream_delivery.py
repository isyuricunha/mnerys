"""Trusted Gateway boundary for choosing direct vs. detached chat delivery.

Only server-issued admission authorizes a turn. Client runtime hints select
disconnect behavior but cannot grant session access, tool authority or change
ownership. The raw SSE buffer stays process-local; durable Web audit is metadata only.
"""
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator, Optional

from src.gateway_admission import AdmittedSession, require_admitted_session
from src.turn_service import TurnService


@dataclass(frozen=True)
class TurnDelivery:
    """In-process stream handle; no new event registry or wire contract."""

    events: AsyncIterator[str]
    detached: bool
    run_id: Optional[str] = None


def should_detach_stream(
    *,
    compare_mode: bool,
    client_runtime_context: Any,
) -> bool:
    """Preserve resumability only for interactive streams.

    Compare panes and explicitly unattended native streams stay tied to the
    HTTP response. Client-supplied context is not an authorization source.
    """
    if compare_mode:
        return False
    context = (
        client_runtime_context
        if isinstance(client_runtime_context, dict)
        else {}
    )
    return not (
        context.get("surface") == "odysseus-native"
        and context.get("unattended_mode") is True
    )


def open_admitted_stream(
    admission: AdmittedSession,
    turns: TurnService,
    stream: AsyncGenerator[str, None],
    *,
    compare_mode: bool,
    client_runtime_context: Any,
    records=None,
    incarnation_id: str = "",
) -> TurnDelivery:
    """Check admission before dispatching to direct or detached lifecycle.

    Direct delivery does not register a detached run; disconnect closes the
    generator via the existing Web/Starlette transport. Detached delivery
    reuses TurnService with exact-run resume/stop semantics.
    """
    session_id = require_admitted_session(admission)
    if not should_detach_stream(
        compare_mode=compare_mode,
        client_runtime_context=client_runtime_context,
    ):
        return TurnDelivery(events=stream, detached=False)

    if records is None and isinstance(client_runtime_context, dict):
        records = client_runtime_context.get("_web_records")
        incarnation_id = incarnation_id or client_runtime_context.get("_incarnation_id", "")

    if records is not None and admission.principal.owner_id and incarnation_id:
        from src.gateway_web_recovery import WebRunJournal
        journal = WebRunJournal(records, owner=admission.principal.owner_id,
                                session_id=session_id, incarnation_id=incarnation_id)
        run = turns.start_detached(session_id, journal.wrap(stream),
                                   run_id=journal.turn_id, on_drained=journal.drained)
    else:
        run = turns.start_detached(session_id, stream)
    return TurnDelivery(events=run.events, detached=True, run_id=run.run_id)
