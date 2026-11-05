"""HTTP-only adapter for an explicitly gated strict-text Web/Core turn.

No independent Agent engine, transport authority, storage or permission
policy. The existing /api/chat_stream route owns authenticated admission and
feature eligibility; this module only binds legacy server dependencies and
serializes the original detached typed turn.
"""

from collections.abc import AsyncIterator
import json
from typing import Any

from fastapi.responses import StreamingResponse

from routes.chat_helpers import LegacyChatContextPorts
from src.gateway_admission import AdmittedSession
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_text_persistence import TrustedTextPersistence
from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.gateway_web_compat_events import serialize_plain_text_web_sse


def _headers(run_id: str) -> dict[str, str]:
    return {
        "X-Odysseus-Run-Id": run_id,
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }


def start_strict_text_web_response(
    *,
    gateway: GatewayTurns,
    admission: AdmittedSession,
    session: Any,
    text: str,
    session_manager: Any,
    chat_handler: Any,
    chat_processor: Any,
    privileges: dict,
    assistant_writer: Any,
    interrupted_writer: Any,
) -> StreamingResponse:
    """Run an admitted, persisted, no-tools text turn in the inherited engine."""
    owner = owner_for_admitted_context(admission, session, session.id)
    delivery = gateway.open_composed_detached_text_events(
        admission, session,
        context_owner=owner,
        request=TextTurnRequest(text),
        ports=LegacyChatContextPorts(),
        chat_handler=chat_handler,
        chat_processor=chat_processor,
        settings=TrustedTextTurnSettings(
            privileges=privileges,
            interaction_mode="chat",
            max_rounds=1,
        ),
        persistence=TrustedTextPersistence(
            session_manager=session_manager,
            saver=assistant_writer,
            interrupted_writer=interrupted_writer,
            emit_message_saved_events=True,
        ),
        web_strict_text=True,
    )

    async def frames() -> AsyncIterator[str]:
        yield "data: " + json.dumps({
            "type": "turn_mode", "mode": "chat", "auto_escalated": False,
        }) + "\n\n"
        yield "data: " + json.dumps({
            "type": "model_info", "model": session.model,
        }) + "\n\n"
        async for item in serialize_plain_text_web_sse(
            delivery.events, replay=True,
            prior_saved_receipt=delivery.prior_saved_receipt,
        ):
            yield item

    return StreamingResponse(
        frames(), media_type="text/event-stream",
        headers=_headers(delivery.run_id),
    )


def resume_strict_text_web_response(
    *,
    gateway: GatewayTurns,
    admission: AdmittedSession,
    session: Any,
    expected_run_id: str,
    after_cursor: str | None,
) -> StreamingResponse | None:
    """Resume one exact already-admitted process-local typed run."""
    owner = owner_for_admitted_context(admission, session, session.id)
    delivery = gateway.resume_composed_detached_text_events(
        admission, session, context_owner=owner,
        expected_run_id=expected_run_id, after_cursor=after_cursor,
        web_strict_text=True,
    )
    if delivery is None:
        return None

    async def frames() -> AsyncIterator[str]:
        async for item in serialize_plain_text_web_sse(
            delivery.events, replay=True,
            prior_saved_receipt=delivery.prior_saved_receipt,
        ):
            yield item

    return StreamingResponse(
        frames(), media_type="text/event-stream",
        headers=_headers(delivery.run_id),
    )
