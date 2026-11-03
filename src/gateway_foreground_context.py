"""Per-route, non-persistent Chat context shaping behind admitted turn policy.

A fallback must start from the same route-neutral source history rather than
reusing a summary/trim for another model's context window. Only the answering
route's staged compaction is later committed by the inherited Web lifecycle.
"""
from typing import Any

from src.context_compactor import maybe_compact, trim_for_context
from src.gateway_admission import AdmittedSession, AdmissionDenied
from src.gateway_foreground_routes import (
    AdmittedForegroundPolicy,
    require_admitted_foreground_policy,
)
from src.model_context import estimate_tokens


def admitted_candidate_request_factory(
    admission: AdmittedSession,
    prepared: AdmittedForegroundPolicy,
    messages: list,
    fallback_context_length: int = 0,
    *,
    session: Any,
):
    """Prepare each candidate's prompt under the checked session owner.

    This is the pre-existing Chat compactor with a server-derived owner and
    session-binding check. It does not persist compaction until the Web adapter
    chooses the answering candidate.
    """
    policy = require_admitted_foreground_policy(admission, prepared)
    if getattr(session, "id", None) != admission.session_id:
        raise AdmissionDenied(403, "Context session does not match admission")
    if not policy.enabled:
        raise AdmissionDenied(403, "Fallback context requires enabled policy")

    owner = admission.principal.owner_id
    state = {
        "requests": {},
        "context_lengths": {},
        "trim_stats": {},
        "compactions": {},
        "was_compacted": {},
    }

    async def factory(index, candidate_url, candidate_model, candidate_headers):
        compaction_state = {}
        candidate_messages, context_length, was_compacted = await maybe_compact(
            session,
            candidate_url,
            candidate_model,
            list(messages),
            candidate_headers,
            owner=owner,
            persist=False,
            compaction_state=compaction_state,
        )
        if not context_length:
            context_length = fallback_context_length
        request_messages = trim_for_context(candidate_messages, context_length)
        state["requests"][index] = request_messages
        state["context_lengths"][index] = context_length
        state["compactions"][index] = compaction_state
        state["was_compacted"][index] = was_compacted
        state["trim_stats"][index] = {
            "messages_before": len(messages),
            "messages_after": len(request_messages),
            "tokens_before": estimate_tokens(messages),
            "tokens_after": estimate_tokens(request_messages),
        }
        return {"messages": request_messages}

    return factory, state
