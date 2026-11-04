"""Owner-admitted reuse of the inherited assistant-response save operation.

M2.1 compatibility boundary: the Web adapter continues to supply its trusted
legacy saver and session manager, which retain their exact transactions,
attachment handling, metadata normalization, incognito behavior, and message
IDs. This wrapper changes no persistence implementation or schema. It is not
an atomic user+assistant turn commit or an incarnation-fenced writer.
"""

from collections.abc import Callable
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context


def save_admitted_assistant_response(
    admission: AdmittedSession,
    session: Any,
    session_manager: Any,
    claimed_session_id: str,
    full_response: str,
    last_metrics: dict | None,
    *,
    context_owner: str | None,
    saver: Callable[..., Any],
    character_name: str | None = None,
    web_sources: list | None = None,
    rag_sources: list | None = None,
    research_sources: list | None = None,
    used_memories: list | None = None,
    do_research: bool = False,
    tool_events: list | None = None,
    incognito: bool = False,
) -> Any:
    """Check issued admission and owner before using the original saver.

    Only server-owned callers may inject the saver and session_manager.
    The trusted original writer is the sole persistence implementation:
    metadata, response stripping and database IDs come from it unchanged.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner or claimed_session_id != session_id:
        raise AdmissionDenied(403, "Assistant save session/owner does not match admission")
    if session_manager is None or not callable(saver):
        raise AdmissionDenied(403, "Missing trusted assistant persistence writer")
    if not isinstance(full_response, str):
        raise AdmissionDenied(403, "Assistant content must be text")
    if last_metrics is not None and not isinstance(last_metrics, dict):
        raise AdmissionDenied(403, "Invalid assistant result metadata")
    for value in (do_research, incognito):
        if type(value) is not bool:
            raise AdmissionDenied(403, "Invalid assistant persistence mode")
    if character_name is not None and not isinstance(character_name, str):
        raise AdmissionDenied(403, "Invalid assistant character metadata")
    for sources in (web_sources, rag_sources, research_sources, used_memories, tool_events):
        if sources is not None and not isinstance(sources, list):
            raise AdmissionDenied(403, "Invalid assistant source metadata")

    return saver(
        session, session_manager, session_id, full_response, last_metrics,
        character_name=character_name,
        web_sources=web_sources,
        rag_sources=rag_sources,
        research_sources=research_sources,
        used_memories=used_memories,
        do_research=do_research,
        tool_events=tool_events,
        incognito=incognito,
    )
