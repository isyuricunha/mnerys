"""M1 opt-in cache reconciliation for a confirmed, incarnation-fenced DB write.

This is NOT connected to the legacy SessionManager; existing cached sessions
have no revision/incarnation fields. Call only after a confirmed DB commit.
Cache conflicts require an authoritative reload, never blind append/retry.
"""

from typing import Any

from src.gateway_admission import AdmittedSession, require_admitted_session
from src.session_write_fencing import FencedDeliveryReceipt, SessionWriteFence


def apply_committed_message_to_cache(
    admission: AdmittedSession,
    cached: Any,
    *,
    previous_fence: SessionWriteFence,
    receipt: FencedDeliveryReceipt,
    message: Any,
) -> bool:
    """Update a fully-hydrated cache only for a matching committed insertion.

    A non-inserted (idempotent replay), stale or incomplete cache is NOT
    modified and must be reloaded from the database. Do not use this function
    as a permission guard; admission and write ownership belong to the DB.
    """
    sid = require_admitted_session(admission)
    if (
        not isinstance(previous_fence, SessionWriteFence)
        or not isinstance(receipt, FencedDeliveryReceipt)
        or not receipt.inserted
        or previous_fence.session_id != sid
        or receipt.fence.session_id != sid
        or receipt.fence.incarnation_id != previous_fence.incarnation_id
        or receipt.fence.revision != previous_fence.revision + 1
        or not isinstance(receipt.message_id, str)
        or not receipt.message_id.strip()
    ):
        return False
    if cached is None or getattr(cached, "id", None) != sid:
        return False
    if (
        getattr(cached, "incarnation_id", None) != previous_fence.incarnation_id
        or getattr(cached, "revision", None) != previous_fence.revision
    ):
        return False
    if (
        admission.principal.owner_id is not None
        and getattr(cached, "owner", None) != admission.principal.owner_id
    ):
        return False
    history = getattr(cached, "history", None)
    count = getattr(cached, "message_count", None)
    if (
        not isinstance(history, list)
        or type(count) is not int
        or count != len(history)
        or not isinstance(getattr(message, "role", None), str)
    ):
        return False
    metadata = getattr(message, "metadata", None)
    if metadata is not None and not isinstance(metadata, dict):
        return False

    # All rejection conditions above are checked before modifying either the
    # caller-owned message or cached session.
    if metadata is None:
        metadata = {}
        message.metadata = metadata
    metadata["_db_id"] = receipt.message_id
    history.append(message)
    cached._history = history
    cached.message_count = count + 1
    cached.revision = receipt.fence.revision
    return True
