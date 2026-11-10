"""M2.1 internal decision boundary for admitted, interactive tool approvals.

The transport authenticates the human request before this call. The Gateway
rechecks the admitted owner/session and delegates destructive, one-use
consumption to the existing sealed in-memory ToolApprovalStore. This is not
durable approval persistence or a remote approval API.
"""

import logging
from dataclasses import dataclass
from typing import Any
from src.agent_core_events import CoreApprovalEvent
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.tool_approvals import ExactToolApproval, PendingToolApproval, ToolApprovalStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolApprovalDecision:
    """Server-only continuation state, never a response payload or grant."""

    decision: str
    pending: PendingToolApproval
    exact: ExactToolApproval | None

    @property
    def event(self) -> CoreApprovalEvent:
        """Observation of the already-consumed decision, not a new grant."""
        return CoreApprovalEvent(
            "approval.decided", self.pending.approval_id, self.decision,
        )

    @property
    def tainted(self) -> bool:
        return bool(self.pending.external_untrusted_context_seen)


def consume_admitted_tool_approval(
    admission: AdmittedSession,
    approval_id: Any,
    decision: Any,
    *,
    plan_mode: bool,
    internal_tool_request: bool,
    store: ToolApprovalStore,
    records: Any = None,
    session: Any = None,
) -> ToolApprovalDecision:
    """Validate and consume an existing sealed action for one admitted session.

    A failed check must not consume another owner's pending approval. The
    store owns the atomic destructive consumption; the Gateway never
    reconstructs a sealed action or treats an approval as new model/tool
    authorization. With ``records`` (a SqlTurnRecords) and the owning
    ``session``, the sealed decision is durably recorded after consumption
    against the original owner, session, turn, tool, operation digest and
    resource identity; recording is best-effort audit and never retries or
    unbinds the in-memory decision.
    """
    pending, normalized_decision = inspect_admitted_tool_approval(
        admission, approval_id, decision, plan_mode=plan_mode,
        internal_tool_request=internal_tool_request, store=store,
    )
    session_id = require_admitted_session(admission)
    owner = admission.principal.owner_id

    exact = store.consume(
        approval_id,
        decision=normalized_decision,
        owner=owner,
        session_id=session_id,
    )
    if normalized_decision in {"approve", "approve_task"} and exact is None:
        raise AdmissionDenied(409, "This tool approval could not be consumed.")

    settle = getattr(records, "settle_web_approval", None)
    if callable(settle) and session is not None:
        from sqlalchemy.exc import SQLAlchemyError
        from src.gateway_turn_records import TurnRecordError

        try:
            settled = settle(owner=owner, session_id=session_id,
                             incarnation_id=getattr(session, "incarnation_id", "") or "",
                             pending=pending, decision=normalized_decision)
        except TurnRecordError as exc:
            raise AdmissionDenied(409, "This approval is no longer valid for the session.") from exc
        except SQLAlchemyError as exc:
            raise AdmissionDenied(503, "Approval audit unavailable; request a fresh turn.") from exc
        if settled:
            return ToolApprovalDecision(normalized_decision, pending, exact)

    if records is not None and exact is not None:
        from datetime import datetime, timezone

        from src.gateway_turn_records import TurnRecordError

        try:
            incarnation = getattr(session, "incarnation_id", None)
            exact_pending = exact.pending
            records.record_approval(
                approval_id=str(approval_id), owner=owner,
                session_id=session_id,
                incarnation_id=incarnation if isinstance(incarnation, str) else "",
                turn_id=exact_pending.origin_run_id,
                tool_name=exact_pending.tool_name,
                operation_digest=exact_pending.digest,
                resource_identity=exact_pending.document_id or None,
                effective_authority=(
                    f"admitted:{owner}:{session_id}"
                ),
                expires_at=(
                    datetime.fromtimestamp(exact_pending.expires_at, tz=timezone.utc)
                    if exact_pending.expires_at and exact_pending.expires_at > 0 else None
                ),
            )
            records.decide_approval(
                approval_id=str(approval_id), owner=owner,
                decision=normalized_decision,
            )
        except TurnRecordError:
            # The in-memory decision was already consumed; the durable
            # record is an audit trail that must not block execution.
            logger.warning(
                "Durable approval recording failed for %s", approval_id,
                exc_info=True,
            )
    return ToolApprovalDecision(normalized_decision, pending, exact)


def inspect_admitted_tool_approval(
    admission: AdmittedSession,
    approval_id: Any,
    decision: Any,
    *,
    plan_mode: bool,
    internal_tool_request: bool,
    store: ToolApprovalStore,
) -> tuple[PendingToolApproval, str]:
    """Validate a pending decision without consuming it or issuing a grant.

    Consumption repeats these checks after asynchronous turn preparation.
    The returned record must never be deserialized from client input.
    """
    session_id = require_admitted_session(admission)
    if type(plan_mode) is not bool or type(internal_tool_request) is not bool:
        raise AdmissionDenied(403, "Invalid server tool approval context")
    if admission.principal.delegated or internal_tool_request:
        raise AdmissionDenied(403, "Tool approvals require an interactive user request")

    owner = admission.principal.owner_id
    pending = store.peek(approval_id)
    if (
        pending is None
        or not isinstance(pending, PendingToolApproval)
        or pending.owner != str(owner or "").strip().casefold()
        or pending.session_id != session_id
    ):
        raise AdmissionDenied(
            409, "This tool approval is invalid, expired, or belongs to another thread."
        )

    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approve", "approve_task", "deny"}:
        raise AdmissionDenied(400, "Invalid tool approval decision.")
    if plan_mode:
        raise AdmissionDenied(
            409, "Tool approvals cannot be consumed while plan mode is active."
        )

    return pending, normalized_decision
