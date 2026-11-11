"""M2.1 admission-bound Web intent reconciliation before final tool policy.

Reuses the original opt-in Web toggle policy and legacy route restrictions.
Server-owned history/context predicates are evaluated lazily and only after
session admission. This service composes candidate tool denials; it never
grants tool execution or bypasses the final account/global policy veto.
"""

from dataclasses import dataclass
import re
from typing import Any, Callable

from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.tool_policy import (
    WEB_TOOL_NAMES,
    is_web_search_explicitly_denied,
    web_intent_may_enable_for_turn,
)


@dataclass(frozen=True)
class AdmittedWebIntent:
    """Original search toggle and denylist after trusted Web intent routing."""

    explicit_web_intent: bool
    contextual_web_turn_followup: bool
    search_enabled: bool
    allow_web_search: Any
    disabled_tools: frozenset[str]


def reconcile_admitted_web_intent(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    message: str,
    explicit_web_intent: bool,
    personal_store_intent: bool,
    tool_intent_is_web: bool,
    capabilities: frozenset[str],
    clean_route_requested: bool,
    allow_web_search: Any,
    search_enabled: bool,
    explicit_browser_intent: bool,
    local_browser_render_intent: bool,
    exact_url_fetch_intent: bool,
    use_turn_contract: bool,
    full_schema_route: bool,
    workspace_bound: bool,
    base_disabled_tools: set[str] | frozenset[str],
    web_link_followup_lookup: Callable[[], bool],
    contextual_web_followup_lookup: Callable[[], bool],
    recent_web_event_lookup: Callable[[], bool],
    message_denies_lookup: Callable[[], bool],
    native_agent_catalog: bool = False,
) -> AdmittedWebIntent:
    """Apply inherited Web precedence with issued session/owner binding.

    All flags and lookup callbacks must be server-derived. In particular,
    'clean_route_requested' does not come directly from the client and is not
    a permission grant. The later final policy and request authority still
    enforce account, bearer, Plan, global and explicit per-request denials.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Web intent context owner does not match admission")
    if (
        not isinstance(message, str)
        or not isinstance(capabilities, frozenset)
        or not all(isinstance(family, str) for family in capabilities)
        or not isinstance(base_disabled_tools, (set, frozenset))
    ):
        raise AdmissionDenied(403, "Invalid trusted Web intent input")
    for flag in (
        explicit_web_intent, personal_store_intent, tool_intent_is_web,
        clean_route_requested, search_enabled, explicit_browser_intent,
        local_browser_render_intent, exact_url_fetch_intent,
        use_turn_contract, full_schema_route, workspace_bound,
        native_agent_catalog,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted Web intent flag")
    for port in (
        web_link_followup_lookup, contextual_web_followup_lookup,
        recent_web_event_lookup, message_denies_lookup,
    ):
        if not callable(port):
            raise AdmissionDenied(403, "Missing trusted Web intent lookup")

    def denied_lookup() -> bool:
        denied = message_denies_lookup()
        if type(denied) is not bool:
            raise AdmissionDenied(403, "Invalid trusted Web denial evidence")
        return denied

    if native_agent_catalog:
        # Unified Agent: search/fetch are available as authorized tools, not
        # enabled by an untrusted composer checkbox. No lexical pre-search.
        # Keep explicit user no-Web requests binding and preserve *all* server
        # policy/owner disabled tools. This is NOT executable tool authority.
        denied = denied_lookup()
        disabled = set(base_disabled_tools)
        if denied:
            disabled.update(WEB_TOOL_NAMES)
        return AdmittedWebIntent(
            explicit_web_intent=bool(explicit_web_intent and not denied),
            contextual_web_turn_followup=False,
            search_enabled=False,
            allow_web_search=None,
            disabled_tools=frozenset(disabled),
        )

    # Keep the exact legacy ordering: past-chat search is personal, but a
    # later explicit classified Web request can still be handled separately.
    past_chat_search = bool(
        re.search(r"\b(?:search|find|look\s*up)\b", message, re.IGNORECASE)
        and re.search(
            r"\b(?:prior|past|previous|old)\s+(?:chats?|sessions?|conversations?)\b",
            message, re.IGNORECASE,
        )
    )
    effective_explicit_web = False if past_chat_search else explicit_web_intent
    effective_explicit_web = effective_explicit_web or bool(
        tool_intent_is_web and not personal_store_intent and not past_chat_search
    )

    # These ports use the same session history and typed successful-tool
    # events the Web adapter already checks. Preserve short-circuit evaluation.
    web_link_followup = web_link_followup_lookup()
    if type(web_link_followup) is not bool:
        raise AdmissionDenied(403, "Invalid trusted Web link evidence")
    contextual_web_turn = False
    if "search_browser" in capabilities:
        contextual_web_turn = contextual_web_followup_lookup()
        if type(contextual_web_turn) is not bool:
            raise AdmissionDenied(403, "Invalid trusted contextual Web evidence")
        if contextual_web_turn:
            contextual_web_turn = recent_web_event_lookup()
            if type(contextual_web_turn) is not bool:
                raise AdmissionDenied(403, "Invalid trusted prior Web tool event")
            if contextual_web_turn:
                contextual_web_turn = not denied_lookup()

    clean_web_intent = bool(
        clean_route_requested
        and "search_browser" in capabilities
        and not denied_lookup()
    )
    if (
        (effective_explicit_web or web_link_followup
         or contextual_web_turn or clean_web_intent)
        and web_intent_may_enable_for_turn(
            None if (contextual_web_turn or clean_web_intent) else allow_web_search,
            message_denies_lookup=denied_lookup(),
        )
    ):
        search_enabled = True
        allow_web_search = "true"

    disabled = set(base_disabled_tools)
    if is_web_search_explicitly_denied(allow_web_search) or not search_enabled:
        disabled.update(WEB_TOOL_NAMES)
        if not explicit_browser_intent:
            disabled.add("youtube_tool")
        if not (explicit_browser_intent or local_browser_render_intent):
            disabled.add("private_browser")
    if exact_url_fetch_intent:
        # An exact public URL allows only the specific reader before the
        # final server-owned account/global policy; not unrestricted search.
        disabled.discard("web_fetch")

    if effective_explicit_web and not use_turn_contract and not full_schema_route:
        # Preserve legacy non-contract Web containment. Combined workspace
        # deliverables retain native output tools only for a bound workspace.
        disabled.update({
            "search_chats", "manage_skills", "manage_memory",
            "create_document", "edit_document", "update_document",
            "send_email", "reply_to_email",
            "manage_notes", "manage_calendar", "manage_tasks", "api_call",
        })
        web_workspace_output = bool(
            workspace_bound
            and re.search(r"(?:^|\s)/workspace/[^\s]+", message)
            and re.search(
                r"(?:\b(?:create|generate|save|write|render|export|produce|build|make)\b|"
                r"创建|生成|保存|写入|制作|截取|剪辑|拼接|导出)",
                message,
                re.IGNORECASE,
            )
        )
        if not web_workspace_output:
            disabled.update({"bash", "python", "read_file", "write_file", "edit_file"})
        if search_enabled:
            disabled.difference_update(WEB_TOOL_NAMES)
        else:
            disabled.update(WEB_TOOL_NAMES)
    elif search_enabled:
        disabled.difference_update(WEB_TOOL_NAMES)

    return AdmittedWebIntent(
        explicit_web_intent=effective_explicit_web,
        contextual_web_turn_followup=bool(contextual_web_turn),
        search_enabled=search_enabled,
        allow_web_search=allow_web_search,
        disabled_tools=frozenset(disabled),
    )
