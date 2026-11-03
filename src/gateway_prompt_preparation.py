"""M2.1 admitted final prompt preparation after inherited context assembly.

The server-side ChatContext builder still owns preprocessing, memory, RAG,
attachments and persistence. The Gateway owns the last authority-bound prompt
selection before model execution: internal-tool data stays untrusted, route-
neutral history is chosen consistently, and a detached stream cannot
silently use a different user's context.
"""

import logging
from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
)
from src.gateway_foreground_routes import (
    AdmittedForegroundPolicy,
    require_admitted_foreground_policy,
)
from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)


def _check_context(
    admission: AdmittedSession,
    foreground_policy: AdmittedForegroundPolicy,
    context: Any,
) -> None:
    require_admitted_foreground_policy(admission, foreground_policy)
    if getattr(context, "user", object()) != admission.principal.owner_id:
        raise AdmissionDenied(403, "Prompt context owner does not match admission")
    if not isinstance(getattr(context, "messages", None), list):
        raise AdmissionDenied(403, "Invalid server prompt context")
    routed = getattr(context, "route_messages", None)
    if routed is not None and not isinstance(routed, list):
        raise AdmissionDenied(403, "Invalid route-neutral prompt context")


def append_admitted_internal_context(
    admission: AdmittedSession,
    foreground_policy: AdmittedForegroundPolicy,
    context: Any,
    message: str,
    *,
    internal_tool_request: bool,
) -> None:
    """Append a guarded tool-loopback message to both prompt histories once."""
    _check_context(admission, foreground_policy, context)
    if type(internal_tool_request) is not bool:
        raise AdmissionDenied(403, "Invalid trusted internal-tool classification")
    if not internal_tool_request:
        return
    tagged = untrusted_context_message("internal tool request", message)
    context.messages.append(tagged)
    routed = getattr(context, "route_messages", None)
    if routed is not None and routed is not context.messages:
        routed.append(tagged)


def _plain_text(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content or "")


def _ensure_latest_request(messages: list, current_message: str) -> list:
    """Preserve the inherited detached-stream mismatch behavior without logging input."""
    current = str(current_message or "").strip()
    if not current:
        return messages
    latest = ""
    for item in reversed(messages or []):
        if isinstance(item, dict) and item.get("role") == "user":
            latest = _plain_text(item.get("content")).strip()
            break
    if latest == current or current in latest or latest in current:
        return messages
    # The old Web helper logged excerpts of both prompt texts. That can
    # disclose private conversation content; log only the condition.
    logger.warning("Detached stream user-context mismatch; appending current request")
    repaired = list(messages or [])
    repaired.append({"role": "user", "content": current})
    return repaired


def select_admitted_prompt_messages(
    admission: AdmittedSession,
    foreground_policy: AdmittedForegroundPolicy,
    context: Any,
    request_text: str,
    *,
    streaming: bool,
    approval_continuation: bool,
) -> list:
    """Select the existing Chat history under admitted owner and fallback policy.

    Non-streaming Chat historically sends ctx.messages in strict mode and
    ctx.route_messages when fallback is enabled. Streaming Chat first checks
    detached request alignment, except that exact approval continuations
    must *never* append an unrelated composer message as fresh consent.
    """
    _check_context(admission, foreground_policy, context)
    if type(streaming) is not bool or type(approval_continuation) is not bool:
        raise AdmissionDenied(403, "Invalid server prompt selection flags")
    source = (
        getattr(context, "route_messages", context.messages)
        if foreground_policy.enabled
        else context.messages
    )
    if source is None or not isinstance(source, list):
        raise AdmissionDenied(403, "Invalid route-neutral prompt context")
    if not streaming:
        return source
    if approval_continuation:
        return list(source)
    return _ensure_latest_request(source, request_text)
