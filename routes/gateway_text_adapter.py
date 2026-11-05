"""HTTP-only adapter for an explicitly gated strict-text Web/Core turn.

No independent Agent engine, transport authority, storage or permission
policy. The existing /api/chat_stream route owns authenticated admission and
feature eligibility; this module only binds legacy server dependencies and
serializes the original detached typed turn.
"""

from collections.abc import AsyncIterator, Callable
import json
from typing import Any

from fastapi.responses import StreamingResponse

from routes.chat_helpers import LegacyChatContextPorts
from src.gateway_admission import AdmittedSession, AdmissionDenied
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
    post_response_runner: Callable[..., None] | None = None,
    memory_manager: Any = None,
    memory_vector: Any = None,
    webhook_manager: Any = None,
    after_user_saved: Callable[[], None] | None = None,
) -> StreamingResponse:
    """Run an admitted, persisted, no-tools text turn in the inherited engine."""
    owner = owner_for_admitted_context(admission, session, session.id)
    if (
        (post_response_runner is not None and not callable(post_response_runner))
        or (after_user_saved is not None and not callable(after_user_saved))
    ):
        raise AdmissionDenied(403, "Invalid trusted Web completion callbacks")
    ports = LegacyChatContextPorts()

    def after_assistant_saved(
        response: str, metrics: dict[str, int] | None,
    ) -> None:
        if post_response_runner is None:
            return
        # The original Gateway service checks admission and owner again.
        # This hook runs *only after* the inherited assistant writer has
        # confirmed its fresh receipt, inside the original detached run.
        # Subscriber reconnection never schedules another response job.
        gateway.dispatch_post_response(
            admission, session, session_manager,
            context_owner=owner, task_runner=post_response_runner,
            message=text, full_response=response, last_metrics=metrics,
            preferences=ports.load_preferences(owner),
            memory_manager=memory_manager, memory_vector=memory_vector,
            webhook_manager=webhook_manager,
            tools_blocked=True, tool_approval_continuation=False,
            incognito=False, compare_mode=False,
            agent_rounds=0, agent_tool_calls=0, extract_skills=False,
        )

    delivery = gateway.open_composed_detached_text_events(
        admission, session,
        context_owner=owner,
        request=TextTurnRequest(text),
        ports=ports,
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
            after_user_saved=after_user_saved,
            after_assistant_saved=(
                after_assistant_saved if post_response_runner is not None else None
            ),
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
