"""HTTP transport adapter for the deliberately narrow notes_v1 Gateway cutover.

Admission, privileges and interactive human consent are verified by the
inherited Web route before this adapter is called. The Gateway remains the
sole executor, owner of approvals and keeper of process-local run state.
"""

from typing import Any, Callable
import logging

from fastapi.responses import StreamingResponse

from routes.chat_helpers import LegacyChatContextPorts
from src.gateway_admission import AdmittedSession, AdmissionDenied
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_text_persistence import (
    TrustedTextPersistence, require_persisted_native_review_origin,
)
from src.gateway_notes_decision_fence import NotesDecisionFence
from src.gateway_notes_event_journal import NotesEventJournal, NotesJournalEvent
from src.gateway_web_approval_cards import read_admitted_pending_review
from src.gateway_turn_composer import (
    TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings,
)
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore

logger = logging.getLogger(__name__)


def server_notes_decision_fence() -> NotesDecisionFence:
    """Resolve only the configured application engine; never auto-create tables.

    The operator must install the reviewed schema before opting in. A missing
    migration aborts before a new user message can be written.
    """
    from core.database import engine

    fence = NotesDecisionFence(engine)
    fence.require_schema()
    return fence


def server_notes_event_journal() -> NotesEventJournal:
    """Resolve the same trusted engine with an explicitly installed schema."""
    from core.database import engine

    journal = NotesEventJournal(engine)
    journal.require_schema()
    return journal


def read_admitted_notes_event_journal(
    *,
    admission: AdmittedSession,
    session: Any,
    journal: NotesEventJournal,
    after_cursor: str = "",
    limit: int = 50,
) -> tuple[NotesJournalEvent, ...]:
    """Only a current interactive owner may replay safe metadata milestones.

    This endpoint does not revive a signed approval and cannot replay tool
    calls, user-visible text or original SSE cursors.
    """
    if admission.principal.delegated:
        raise AdmissionDenied(403, "Native notes event replay requires a human session")
    owner = owner_for_admitted_context(admission, session, session.id)
    history = getattr(session, "history", None)
    if not isinstance(history, list) or not history:
        return ()
    latest = history[-1]
    # A confirmed assistant response can follow the original user request.
    # Read that just-finished request, but never reach further back through a
    # newer user turn, another tool message or an uncommitted assistant save.
    if getattr(latest, "role", None) == "assistant" and len(history) >= 2:
        assistant_meta = getattr(latest, "metadata", None)
        if not (
            isinstance(assistant_meta, dict)
            and type(assistant_meta.get("_db_id")) is str
        ):
            return ()
        latest = history[-2]
    metadata = getattr(latest, "metadata", None)
    receipt = metadata.get("_db_id") if isinstance(metadata, dict) else None
    if getattr(latest, "role", None) != "user" or type(receipt) is not str:
        return ()
    return journal.replay(
        owner=owner, session_id=session.id,
        origin_user_receipt=receipt,
        after_cursor=after_cursor, limit=limit,
    )


def require_pending_notes_fence(
    *,
    owner: str,
    session: Any,
    approval_id: str,
    approval_store: ToolApprovalStore,
    decision_fence: NotesDecisionFence,
) -> None:
    """Require a registered, unreserved review with the original user receipt.

    This never grants approval; the Gateway still rechecks identity, exact
    action, human consent and tool policy separately.
    """
    pending = approval_store.peek(approval_id)
    if pending is None or pending.origin_profile != "notes_v1":
        raise AdmissionDenied(409, "Pending notes review is unavailable")
    user_receipt = require_persisted_native_review_origin(session, pending)
    state = decision_fence.status(
        owner=owner, session_id=session.id,
        approval_id=approval_id,
    )
    if state is None or state.state != "pending":
        raise AdmissionDenied(409, "Notes decision has expired or was already reserved")


def read_lost_notes_review_status(
    *,
    admission: AdmittedSession,
    session: Any,
    decision_fence: NotesDecisionFence,
) -> str | None:
    """Read only the latest original user receipt after process-local loss.

    Does not restore a card, sealed action, event cursor, grant or tool runner.
    The admission owner and current committed transcript remain authoritative.
    """
    if admission.principal.delegated:
        return None
    owner = owner_for_admitted_context(admission, session, session.id)
    history = getattr(session, "history", None)
    if not isinstance(history, list) or not history:
        return None
    latest = history[-1]
    metadata = getattr(latest, "metadata", None)
    receipt = metadata.get("_db_id") if isinstance(metadata, dict) else None
    if (
        getattr(latest, "role", None) != "user"
        or type(receipt) is not str or not receipt
    ):
        return None
    return decision_fence.read_only_recovery(
        owner=owner, session_id=session.id,
        origin_user_receipt=receipt,
    )


def _as_http_response(delivery: Any) -> StreamingResponse:
    return StreamingResponse(
        delivery.events,
        media_type="text/event-stream",
        headers={
            "X-Odysseus-Run-Id": delivery.run_id,
            "X-Mnerys-Gateway-Profile": "notes_v1",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def start_restricted_notes_web_response(
    *,
    gateway: GatewayTurns,
    admission: AdmittedSession,
    session: Any,
    request: TextTurnRequest | ToolApprovalTurnRequest,
    session_manager: Any,
    chat_handler: Any,
    chat_processor: Any,
    privileges: dict[str, Any],
    global_disabled_tools: tuple[str, ...],
    assistant_writer: Callable[..., Any],
    interrupted_writer: Callable[..., Any],
    approval_store: ToolApprovalStore,
    decision_fence: NotesDecisionFence,
    event_journal: NotesEventJournal,
    after_user_saved: Callable[[], None] | None = None,
    post_response_runner: Callable[..., None] | None = None,
    memory_manager: Any = None,
    memory_vector: Any = None,
    webhook_manager: Any = None,
) -> StreamingResponse:
    """Start one admitted, persisted, detached native notes turn.

    No remote client selects trusted settings or a tool inventory: the
    server fixes every option, and the composer rechecks the same effective
    account/global tool policy and sealed one-use approval.
    """
    owner = owner_for_admitted_context(admission, session, session.id)
    if (
        not isinstance(request, (TextTurnRequest, ToolApprovalTurnRequest))
        or type(privileges) is not dict
        or type(global_disabled_tools) is not tuple
        or any(type(item) is not str for item in global_disabled_tools)
        or not isinstance(approval_store, ToolApprovalStore)
        or not isinstance(decision_fence, NotesDecisionFence)
        or not isinstance(event_journal, NotesEventJournal)
        or (after_user_saved is not None and not callable(after_user_saved))
        or (post_response_runner is not None and not callable(post_response_runner))
    ):
        raise AdmissionDenied(403, "Invalid trusted native notes Web options")
    # This must happen before a user append or a destructive store.consume.
    # No live database migration is ever attempted here.
    try:
        decision_fence.require_schema()
        event_journal.require_schema()
    except Exception as exc:
        raise AdmissionDenied(503, "Native notes decision ledger is unavailable") from exc
    if "manage_notes" in global_disabled_tools:
        raise AdmissionDenied(403, "Native notes are disabled by server policy")
    if getattr(session, "cwd", None):
        raise AdmissionDenied(403, "Native notes profile does not accept a workspace")
    if isinstance(request, ToolApprovalTurnRequest) and admission.principal.delegated:
        raise AdmissionDenied(403, "Tool approvals require an interactive session")
    # A continuation is a control-plane decision, not a new user message.
    # Recover only the request text already sealed by the original approval
    # store. Never use a new browser message or model-authored text.
    if isinstance(request, ToolApprovalTurnRequest):
        read_admitted_pending_review(
            admission, session, approval_store, request.approval_id,
            context_owner=owner,
        )
        original = approval_store.peek(request.approval_id)
        if original is None or type(original.request_text) is not str:
            raise AdmissionDenied(409, "The original notes request is unavailable")
        require_pending_notes_fence(
            owner=owner, session=session, approval_id=request.approval_id,
            approval_store=approval_store, decision_fence=decision_fence,
        )
        request_text = original.request_text
    else:
        original = None
        request_text = request.message
    ports = LegacyChatContextPorts()

    def register_pending_review(approval_id: str) -> None:
        pending = approval_store.peek(approval_id)
        if pending is None or pending.origin_profile != "notes_v1":
            raise AdmissionDenied(409, "Original pending notes review is unavailable")
        read_admitted_pending_review(
            admission, session, approval_store, approval_id,
            context_owner=owner,
        )
        user_receipt = require_persisted_native_review_origin(session, pending)
        if not decision_fence.issue(
            owner=owner, session_id=session.id, approval_id=approval_id,
            action_digest=pending.digest, origin_user_receipt=user_receipt,
            issued_at=pending.created_at, expires_at=pending.expires_at,
        ):
            raise AdmissionDenied(409, "Pending notes review was not durably registered")
        # The event is recorded by the original detached server writer, not
        # by a subscribing browser. Journal failure stops the actionable card.
        event_journal.append(
            owner=owner, session_id=session.id,
            origin_user_receipt=user_receipt, approval_id=approval_id,
            kind="review_registered",
        )

    def reserve_original_decision(pending: Any, user_receipt: str, decision: str) -> None:
        if (
            pending.origin_profile != "notes_v1"
            or not decision_fence.reserve(
                owner=owner, session_id=session.id,
                approval_id=pending.approval_id,
                action_digest=pending.digest,
                origin_user_receipt=user_receipt,
                decision=decision,
            )
        ):
            raise AdmissionDenied(409, "Notes decision is stale or already reserved")
        # SQL reservation is already irreversible. If journaling fails now,
        # do NOT consume the Core approval or retry the uncertain reservation.
        event_journal.append(
            owner=owner, session_id=session.id,
            origin_user_receipt=user_receipt,
            approval_id=pending.approval_id,
            kind="decision_denied" if decision == "deny" else "decision_reserved",
        )

    def after_assistant_saved(
        response: str, metrics: dict[str, Any] | None,
    ) -> None:
        if original is not None and request.decision != "deny":
            # The legacy saver has already committed a new assistant receipt.
            # A missing fence update after an uncertain crash is fail-closed:
            # its reserved action is NEVER replayed automatically.
            user_receipt = require_persisted_native_review_origin_at_decision
            metadata = getattr(session.history[-1], "metadata", None)
            assistant_receipt = (
                metadata.get("_db_id") if isinstance(metadata, dict) else None
            )
            try:
                if decision_fence.mark_committed(
                    owner=owner, session_id=session.id,
                    approval_id=original.approval_id,
                    action_digest=original.digest,
                    origin_user_receipt=user_receipt,
                    assistant_receipt=assistant_receipt,
                ):
                    event_journal.append(
                        owner=owner, session_id=session.id,
                        origin_user_receipt=user_receipt,
                        approval_id=original.approval_id,
                        kind="assistant_committed",
                    )
                else:
                    logger.warning("Notes decision fence remains reserved after assistant commit")
            except Exception:
                # The assistant is already committed. Never retry its effect
                # solely because optional metadata journaling failed.
                logger.warning("Notes decision fence/journal completion could not be confirmed")
        if post_response_runner is None:
            return
        # The original persisted wrapper calls this once after an assistant
        # receipt has been confirmed and the Agent Loop is closed. The legacy
        # runner keeps its notification, accounting and auto-naming behavior.
        # Do not schedule auxiliary extraction for tool or approval turns.
        gateway.dispatch_post_response(
            admission, session, session_manager,
            context_owner=owner, task_runner=post_response_runner,
            message=request_text, full_response=response, last_metrics=metrics,
            preferences=ports.load_preferences(owner),
            memory_manager=memory_manager, memory_vector=memory_vector,
            webhook_manager=webhook_manager,
            tools_blocked=True,
            tool_approval_continuation=isinstance(request, ToolApprovalTurnRequest),
            incognito=False, compare_mode=False, agent_rounds=0,
            agent_tool_calls=0, extract_skills=False,
        )

    require_persisted_native_review_origin_at_decision = (
        require_persisted_native_review_origin(session, original)
        if original is not None else None
    )

    delivery = gateway.start_detached_persisted_notes_web_sse(
        admission, session, context_owner=owner,
        request=request, ports=ports,
        chat_handler=chat_handler, chat_processor=chat_processor,
        settings=TrustedTextTurnSettings(
            privileges=privileges, global_disabled_tools=global_disabled_tools,
            interaction_mode="agent", max_rounds=2,
            offered_tools=frozenset({"manage_notes"}),
            persist_verified_native_tools=True,
        ),
        approval_store=approval_store,
        persistence=TrustedTextPersistence(
            session_manager=session_manager,
            saver=assistant_writer,
            interrupted_writer=interrupted_writer,
            emit_message_saved_events=True,
            after_user_saved=(
                after_user_saved if isinstance(request, TextTurnRequest) else None
            ),
            after_assistant_saved=after_assistant_saved,
            after_review_prepared=register_pending_review,
            before_approval_consume=reserve_original_decision,
        ),
    )
    return _as_http_response(delivery)


def resume_restricted_notes_web_response(
    *,
    gateway: GatewayTurns,
    admission: AdmittedSession,
    session: Any,
    expected_run_id: str,
    after_cursor: str | None,
    approval_store: ToolApprovalStore,
    decision_fence: NotesDecisionFence,
) -> StreamingResponse | None:
    """Replay an exact owner-admitted native notes run; never restart it."""
    owner = owner_for_admitted_context(admission, session, session.id)
    from src import agent_runs
    run = agent_runs.get_matching_run(session.id, expected_run_id, mode="typed")
    if run is not None and run.web_native_notes is True:
        pending_ids = [
            item.payload.get("approval_id")
            for item in tuple(run.buffer)
            if item.type == "approval.requested"
        ]
        if len(pending_ids) > 1:
            raise AdmissionDenied(409, "Invalid notes review replay history")
        if pending_ids:
            require_pending_notes_fence(
                owner=owner, session=session, approval_id=pending_ids[0],
                approval_store=approval_store, decision_fence=decision_fence,
            )
    delivery = gateway.resume_detached_persisted_notes_web_sse(
        admission, session, context_owner=owner,
        expected_run_id=expected_run_id,
        after_cursor=after_cursor, approval_store=approval_store,
    )
    return None if delivery is None else _as_http_response(delivery)
