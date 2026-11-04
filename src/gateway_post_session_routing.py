"""M2.1 session-admitted routing after model/session recovery.

The inherited Web adapter still resolves actual model profiles, document
ownership and confined workspace paths. This service owns *mode* promotion
and intent state after the session has passed server-issued admission. A
selected route, workspace hint or warm browser event never issues tool grants;
tool policy, bound authority and the original dispatcher remain mandatory.
"""

from dataclasses import dataclass
import re
from typing import Any, Callable

from src.action_intents import ToolIntent
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context


_DOCUMENT_EDIT_PATTERN = re.compile(
    r"\b(?:make|sound|rewrite|revise|rework|edit|update|change|polish|"
    r"professional|fun|formal|casual|shorter|longer|friendlier|warmer|clearer)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AdmittedPostSessionRoute:
    """Owner-bound mode/intent decisions; never a tool authorization grant."""

    chat_mode: str
    auto_escalated: bool
    tool_intent: ToolIntent | None
    workspace_agent_intent: bool
    workspace: str | None
    allow_bash: Any
    explicit_browser_intent: bool
    private_browser_warm: bool
    contextual_browser_followup: bool
    escalation_events: tuple[str, ...]


@dataclass(frozen=True)
class AdmittedDocumentEditRoute:
    """Owner-checked visible document may trigger Agent mode, not a write."""

    chat_mode: str
    auto_escalated: bool
    edit_requested: bool


def _admitted_owner(
    admission: AdmittedSession, session: Any, context_owner: str | None,
) -> None:
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Session route owner does not match admission")


def reconcile_admitted_post_session_route(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    message: Any,
    chat_mode: str,
    auto_escalated: bool,
    tool_intent: ToolIntent | None,
    workspace_agent_intent: bool,
    workspace: str | None,
    allow_bash: Any,
    explicit_browser_intent: bool,
    clean_route_requested: bool,
    recent_private_browser_success_lookup: Callable[[], bool],
    contextual_web_followup_lookup: Callable[[], bool],
    contextual_browser_followup_lookup: Callable[[], bool],
    workspace_from_message_lookup: Callable[[], tuple[str | None, Any]],
) -> AdmittedPostSessionRoute:
    """Preserve the original post-model route precedence and lazy lookups.

    The callback ports must be server-owned, not caller-supplied evidence:
    Web uses the admitted session's typed execution history and its existing
    confined path resolver. No callback is evaluated until ownership passes.
    """
    _admitted_owner(admission, session, context_owner)
    if (
        not isinstance(chat_mode, str)
        or not (tool_intent is None or isinstance(tool_intent, ToolIntent))
        or not (workspace is None or isinstance(workspace, str))
    ):
        raise AdmissionDenied(403, "Invalid trusted session route input")
    for flag in (
        auto_escalated, workspace_agent_intent, explicit_browser_intent,
        clean_route_requested,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted session route flag")
    for lookup in (
        recent_private_browser_success_lookup,
        contextual_web_followup_lookup,
        contextual_browser_followup_lookup,
        workspace_from_message_lookup,
    ):
        if not callable(lookup):
            raise AdmissionDenied(403, "Missing trusted session routing lookup")

    def read_boolean(lookup: Callable[[], bool]) -> bool:
        evidence = lookup()
        if type(evidence) is not bool:
            raise AdmissionDenied(403, "Invalid trusted session routing evidence")
        return evidence

    private_browser_warm = bool(
        clean_route_requested
        and read_boolean(recent_private_browser_success_lookup)
    )
    browser_intent = explicit_browser_intent or private_browser_warm
    mode = chat_mode
    escalated = auto_escalated
    intent = tool_intent
    workspace_intent = workspace_agent_intent
    bound_workspace = workspace
    bash_toggle = allow_bash
    events: list[str] = []

    # A persisted compact-model route takes precedence over generic Web
    # follow-up inference, but the browser referential check still runs.
    if mode == "chat" and clean_route_requested:
        mode = "agent"
        escalated = True
        events.append("clean_route")
    if (
        mode == "chat"
        and isinstance(message, str)
        and (not intent or not intent.needs_tools)
        and read_boolean(contextual_web_followup_lookup)
    ):
        intent = ToolIntent(True, "web", "contextual web lookup follow-up")
        mode = "agent"
        escalated = True
        workspace_intent = False
        events.append("contextual_web")

    contextual_browser = bool(
        isinstance(message, str)
        and read_boolean(contextual_browser_followup_lookup)
    )
    if contextual_browser:
        browser_intent = True
        if mode == "chat":
            mode = "agent"
            escalated = True
            workspace_intent = False
            events.append("contextual_browser")

    # The Web callback must perform the same real path confinement check as
    # before. An omitted workspace still permits the path check in Agent mode.
    if not bound_workspace and isinstance(message, str):
        resolved = workspace_from_message_lookup()
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            raise AdmissionDenied(403, "Invalid server-validated workspace resolution")
        path, _ = resolved
        if path is not None and not isinstance(path, str):
            raise AdmissionDenied(403, "Invalid server-validated workspace path")
        if path:
            bound_workspace = path
            mode = "agent"
            escalated = True
            workspace_intent = True
            bash_toggle = "true"
            events.append("workspace_path")

    return AdmittedPostSessionRoute(
        chat_mode=mode,
        auto_escalated=escalated,
        tool_intent=intent,
        workspace_agent_intent=workspace_intent,
        workspace=bound_workspace,
        allow_bash=bash_toggle,
        explicit_browser_intent=browser_intent,
        private_browser_warm=private_browser_warm,
        contextual_browser_followup=contextual_browser,
        escalation_events=tuple(events),
    )


def promote_admitted_document_edit_route(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    message: Any,
    chat_mode: str,
    auto_escalated: bool,
    active_document: bool,
) -> AdmittedDocumentEditRoute:
    """Preserve the original edit-intent promotion for an owner-checked tab.

    The Web adapter must resolve a visible, owner/session-allowed document
    before setting active_document. This result does not grant write access.
    """
    _admitted_owner(admission, session, context_owner)
    if not isinstance(chat_mode, str):
        raise AdmissionDenied(403, "Invalid trusted document mode")
    if type(auto_escalated) is not bool or type(active_document) is not bool:
        raise AdmissionDenied(403, "Invalid trusted document routing flag")

    edit_requested = bool(
        active_document
        and chat_mode == "chat"
        and isinstance(message, str)
        and _DOCUMENT_EDIT_PATTERN.search(message)
    )
    return AdmittedDocumentEditRoute(
        chat_mode="agent" if edit_requested else chat_mode,
        auto_escalated=auto_escalated or edit_requested,
        edit_requested=edit_requested,
    )
