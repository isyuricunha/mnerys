"""Non-authorizing selector for the guarded, default-disabled notes_v1 route.

Only the original authenticated Web route can supply an admitted session ID.
The selection creates an input request, never a principal, permission, tool
grant or approval decision. Unsupported profiles fail closed at the route.
"""

from collections.abc import Mapping
import re

from src.gateway_turn_composer import TextTurnRequest, ToolApprovalTurnRequest


_PROFILE = "notes_v1"
_APPROVAL = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_BASE_FIELDS = frozenset({
    "gateway_profile", "session", "mode", "no_memory", "use_rag",
    "active_doc_state",
})
_MESSAGE_FIELDS = _BASE_FIELDS | {"message"}
_DECISION_FIELDS = _BASE_FIELDS | {
    "tool_approval_id", "tool_approval_decision", "message",
}
_DECISIONS = frozenset({"approve", "approve_task", "deny"})


def select_restricted_notes_web_input(
    *,
    server_enabled: bool,
    form_values: Mapping,
    json_values: Mapping | None,
    admitted_session_id: str,
) -> TextTurnRequest | ToolApprovalTurnRequest | None:
    """Parse a tightly limited profile; any ambiguity stays on legacy Web.

    Call only after the existing HTTP authentication, session ownership and
    account/model admission. No pending approval is read or consumed here.
    """
    if (
        type(server_enabled) is not bool or not server_enabled
        or not isinstance(form_values, Mapping)
        or (json_values is not None and not isinstance(json_values, Mapping))
        or type(admitted_session_id) is not str or not admitted_session_id
        or len(admitted_session_id) > 256
    ):
        return None

    if form_values and json_values:
        # Never merge two possible authorities or silently prefer one source.
        return None
    values = json_values if json_values is not None else form_values
    if not isinstance(values, Mapping) or not values:
        return None
    if (
        any(type(key) is not str for key in values)
        or any(
            hasattr(values, "getlist") and len(values.getlist(key)) != 1
            for key in values
        )
        or not set(values) <= _DECISION_FIELDS
        or values.get("gateway_profile") != _PROFILE
        or values.get("session") != admitted_session_id
        or values.get("mode") != "agent"
        or values.get("active_doc_state") != "none"
    ):
        return None
    is_json = json_values is not None
    if is_json:
        if values.get("no_memory") is not True or values.get("use_rag") is not False:
            return None
    elif values.get("no_memory") != "true" or values.get("use_rag") != "false":
        return None

    approval_id = values.get("tool_approval_id")
    decision = values.get("tool_approval_decision")
    if approval_id is None and decision is None:
        if set(values) != _MESSAGE_FIELDS:
            return None
        message = values.get("message")
        if type(message) is not str or not message.strip() or len(message) > 16384:
            return None
        return TextTurnRequest(message)

    if (
        set(values) not in (_DECISION_FIELDS, _DECISION_FIELDS - {"message"})
        or (values.get("message") not in (None, ""))
        or type(approval_id) is not str
        or _APPROVAL.fullmatch(approval_id) is None
        or type(decision) is not str or decision not in _DECISIONS
    ):
        return None
    return ToolApprovalTurnRequest(approval_id, decision)
