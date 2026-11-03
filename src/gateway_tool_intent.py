"""M2.1 admitted selection of exact requested tools and bounded continuations.

This service reuses the inherited request classifier. Browser/web follow-up
hints and verified prior execution are supplied by the existing server-owned
Web compatibility adapter; they select schemas but cannot grant execution.
The immutable contract resolver and hardened dispatcher still enforce policy.
"""

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, require_admitted_session,
)
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_tool_inventory import AdmittedToolInventory
from src.tool_policy import ToolPolicy, build_effective_tool_policy, WEB_TOOL_NAMES
from src.turn_contract import (
    preserve_bound_editor_selected_tools,
    requests_independent_web_source,
    selected_tools_for_request,
)

_FIXTURE_PERSONAL_TOOL_FAMILIES = {
    "manage_notes": "notes",
    "manage_calendar": "calendar",
    "manage_tasks": "tasks",
}


@dataclass(frozen=True)
class AdmittedToolIntent:
    """Exact selected tools and required names; never an authority grant."""

    selected_tools: frozenset[str] | None
    required_tools: frozenset[str]
    capabilities: frozenset[str]
    active_capabilities: frozenset[str]
    warm_tools: frozenset[str]
    policy: ToolPolicy
    fixture_personal_tools: frozenset[str]


def select_admitted_tool_intent(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    inventory: AdmittedToolInventory,
    message: str,
    capabilities: Iterable[str],
    active_capabilities: Iterable[str],
    warm_tools: frozenset[str],
    recent_web_tool_lookup: Callable[[], str | None],
    active_document: bool,
    contextual_browser_followup: bool,
    contextual_web_followup: bool,
    active_email_review: bool,
    corrected_browser_target: bool,
) -> AdmittedToolIntent:
    """Apply inherited exact-selection precedence under the admitted owner.

    Contextual flags and prior-tool evidence must be computed by the server
    from the loaded session. They are selection hints, not new permissions:
    inventory policy and request authority still gate all offered tools.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Tool intent owner does not match admission")
    if (
        not isinstance(inventory, AdmittedToolInventory)
        or not isinstance(inventory.policy, ToolPolicy)
        or not isinstance(inventory.disabled_tools, frozenset)
        or not isinstance(inventory.owner_blocked, frozenset)
        or not isinstance(message, str)
        or not isinstance(warm_tools, frozenset)
        or not all(isinstance(name, str) for name in warm_tools)
        or not callable(recent_web_tool_lookup)
    ):
        raise AdmissionDenied(403, "Invalid trusted tool intent input")
    for flag in (
        active_document, contextual_browser_followup, contextual_web_followup,
        active_email_review, corrected_browser_target,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted tool intent flag")
    if (
        not isinstance(capabilities, (frozenset, set))
        or not isinstance(active_capabilities, (frozenset, set))
        or not all(isinstance(name, str) for name in capabilities)
        or not all(isinstance(name, str) for name in active_capabilities)
    ):
        raise AdmissionDenied(403, "Invalid server-resolved capability families")

    # The old Web route's exact selection order is significant: the browser
    # retry takes precedence over an unclassified Web follow-up, and the
    # editor can add writers only when already owner-checked as visible.
    selected = selected_tools_for_request(message)
    if selected is None and contextual_browser_followup:
        selected = frozenset({"private_browser"})
    required = set(selected or ())
    selected = preserve_bound_editor_selected_tools(
        message, selected, active_document=active_document,
    )

    disabled = set(inventory.disabled_tools)
    owner_blocked = set(inventory.owner_blocked)
    fixture_personal = (
        set(selected or ())
        & set(_FIXTURE_PERSONAL_TOOL_FAMILIES)
    ) - disabled - owner_blocked

    routed_capabilities = frozenset(capabilities)
    active = frozenset(active_capabilities)
    contract_policy = inventory.policy
    if str(owner or "").startswith("sft_") and fixture_personal:
        # Fixture-only family restoration never subtracts owner/global denies
        # or grants a tool. Preserve unrelated families in compound requests.
        routed_capabilities = frozenset(
            set(routed_capabilities) | {
                _FIXTURE_PERSONAL_TOOL_FAMILIES[name]
                for name in fixture_personal
            }
        )
        active = routed_capabilities
        contract_policy = build_effective_tool_policy(
            disabled_tools=disabled | owner_blocked,
            last_user_message=message,
        )

    if selected == {"web_search"} and requests_independent_web_source(message):
        prior_web_tool = recent_web_tool_lookup()
        if prior_web_tool is not None and prior_web_tool not in WEB_TOOL_NAMES:
            raise AdmissionDenied(403, "Invalid trusted prior web tool")
        if prior_web_tool in {"web_search", "web_fetch"}:
            # A second credible source needs a page read, not a repeated search.
            selected = {"web_fetch"}

    if selected is None and contextual_web_followup:
        prior_web_tool = recent_web_tool_lookup()
        if prior_web_tool is not None and prior_web_tool not in WEB_TOOL_NAMES:
            raise AdmissionDenied(403, "Invalid trusted prior web tool")
        if prior_web_tool:
            selected = {prior_web_tool}

    if (
        selected is None
        and active_email_review
        and "email" in routed_capabilities
    ):
        # An open email review requires UI control; it does not authorize
        # direct email sending or document creation.
        routed_capabilities = routed_capabilities | {"ui"}
        required.add("ui_control")

    if corrected_browser_target:
        # Recover through a bounded browser/web set, still policy-filtered.
        selected = {"private_browser", "web_fetch", "web_search"}
        required = {"private_browser"}

    return AdmittedToolIntent(
        selected_tools=None if selected is None else frozenset(selected),
        required_tools=frozenset(required),
        capabilities=frozenset(routed_capabilities),
        active_capabilities=frozenset(active),
        warm_tools=warm_tools,
        policy=contract_policy,
        fixture_personal_tools=frozenset(fixture_personal),
    )
