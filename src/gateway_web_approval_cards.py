"""Owner-checked, read-only preview of a *sealed* pending review card.

This does not grant an action or implement a remote approval endpoint. The
actual decision is still admitted, consumed and resource-revalidated by the
original Gateway/ToolApprovalStore; browser-visible card fields are display
copies, never authority.
"""

from dataclasses import dataclass
import json
import re
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.tool_approvals import ExactToolApproval, ToolApprovalStore


_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MAX_CARD_BYTES = 32768


@dataclass(frozen=True, slots=True)
class SealedReviewCard:
    approval_id: str
    digest: str
    payload: dict[str, Any]


def read_admitted_pending_review(
    admission: AdmittedSession,
    session: Any,
    store: ToolApprovalStore,
    approval_id: str,
    *,
    context_owner: str | None,
) -> SealedReviewCard:
    """Read one still-pending exact action without consuming it.

    A stale, superseded, tampered, expired or foreign pending record cannot be
    projected into a Web card. The view includes the complete input for human
    consent: it is JSON-escaped and not a command to execute.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if owner != context_owner or type(owner) is not str or not owner:
        raise AdmissionDenied(403, "Pending review context owner mismatch")
    if (
        not isinstance(store, ToolApprovalStore)
        or type(approval_id) is not str
        or _APPROVAL_ID.fullmatch(approval_id) is None
    ):
        raise AdmissionDenied(403, "Invalid trusted pending review reference")
    pending = store.peek(approval_id)
    if pending is None:
        raise AdmissionDenied(409, "Pending review no longer exists")
    if (
        pending.approval_id != approval_id
        or pending.owner != owner.strip().casefold()
        or pending.session_id != session_id
        or pending.tool_name != "manage_notes"
        or pending.workspace or pending.document_id
        or pending.document_version is not None
        or pending.document_digest
        or type(pending.content) is not str
        or not pending.content
        or len(pending.content.encode("utf-8")) > _MAX_CARD_BYTES
        or type(pending.digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", pending.digest) is None
        or pending.request_authority is None
        or not pending.request_authority.bound_to(
            owner=owner, session_id=session_id,
        )
        or not ExactToolApproval(pending).matches(
            owner=owner, session_id=session_id,
            tool_name=pending.tool_name, content=pending.content,
            workspace=None,
        )
    ):
        raise AdmissionDenied(409, "Pending review differs from its original seal")

    # Reuse the *existing* browser card's options, action and exact input.
    # Never abbreviate the action: the user must see what is being approved.
    payload = pending.public_payload()
    if (
        payload.get("kind") != "tool_approval"
        or payload.get("approval_id") != approval_id
        or payload.get("session_id") != session_id
        or not isinstance(payload.get("options"), list)
        or not isinstance(payload.get("action"), dict)
        or payload["action"].get("content") != pending.content
    ):
        raise AdmissionDenied(409, "Invalid sealed review display")
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_CARD_BYTES:
        raise AdmissionDenied(409, "Pending review cannot be displayed in full")
    # Return an independent display snapshot; neither renderer nor callback
    # gets the original mutable store or the authority-bearing pending object.
    return SealedReviewCard(approval_id, pending.digest, json.loads(encoded))
