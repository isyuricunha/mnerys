"""M2.1 admitted classification and augmentation of initial turn capabilities.

The Gateway reuses the inherited classifier and corrected-browser resolver.
It separates the capabilities necessary for a turn from extra document
capabilities which may be offered but are never required. No candidate family
is an execution grant; the original policy, contract and dispatcher remain
authoritative.
"""

from dataclasses import dataclass
from typing import Any, Callable

from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.turn_contract import corrected_browser_target, requested_capabilities


@dataclass(frozen=True)
class AdmittedTurnCapabilities:
    """Server-selected candidate families before exact tool selection."""

    capabilities: frozenset[str]
    active_capabilities: frozenset[str]
    corrected_browser_target: dict | None
    image_editing_requested: bool
    suppress_explicit_web_intent: bool


def classify_admitted_turn_capabilities(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    message: str,
    use_turn_contract: bool,
    active_document: bool,
    workspace_bound: bool,
    image_attachment: bool,
    explicit_browser_intent: bool,
    external_discovery_intent: bool,
    private_browser_warm: bool,
    contextual_browser_followup_lookup: Callable[[], bool],
) -> AdmittedTurnCapabilities:
    """Keep inherited request/history classification under issued admission.

    The Web adapter must resolve workspace, visible document, image attachment
    and browser intent before invoking this internal service. Those facts only
    select candidate schemas and never bypass owner-scoped tool policy.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Capability context owner does not match admission")
    if not isinstance(message, str) or not callable(contextual_browser_followup_lookup):
        raise AdmissionDenied(403, "Invalid trusted capability input")
    for flag in (
        use_turn_contract, active_document, workspace_bound, image_attachment,
        explicit_browser_intent, external_discovery_intent, private_browser_warm,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted capability flag")

    history = getattr(session, "history", []) or []
    # This URL correction is recognized even outside contract-enabled Agent
    # paths, matching the old Web route; it does not authorize a web tool.
    browser_target = corrected_browser_target(message, history)
    if browser_target is not None and not isinstance(browser_target, dict):
        raise AdmissionDenied(403, "Invalid resolved browser correction")

    base_capabilities = (
        frozenset(requested_capabilities(
            message, history,
            active_document=active_document,
            workspace=workspace_bound,
            image_attachment=image_attachment,
        ))
        if use_turn_contract else frozenset()
    )
    if not all(isinstance(family, str) for family in base_capabilities):
        raise AdmissionDenied(403, "Invalid resolved capability family")

    image_editing_requested = "image_editing" in base_capabilities
    augmented = base_capabilities

    if use_turn_contract and (explicit_browser_intent or external_discovery_intent):
        # A validated interactive navigation/discovery route keeps its browser
        # family even if the lexical classifier stays intentionally conservative.
        augmented = augmented | {"search_browser"}
    if use_turn_contract and not augmented and private_browser_warm:
        contextual = contextual_browser_followup_lookup()
        if type(contextual) is not bool:
            raise AdmissionDenied(403, "Invalid trusted browser follow-up evidence")
        if contextual:
            # A prior browser success cannot contaminate an explicit personal
            # request: only empty classifications may inherit this family.
            augmented = frozenset({"search_browser"})

    active_capabilities = frozenset(augmented)
    if use_turn_contract and active_document:
        # An owner-checked visible editor may be offered without requiring a
        # document mutation when a user asks for an unrelated operation.
        augmented = augmented | {"documents"}

    capabilities = frozenset(augmented)
    return AdmittedTurnCapabilities(
        capabilities=capabilities,
        active_capabilities=active_capabilities,
        corrected_browser_target=browser_target,
        image_editing_requested=image_editing_requested,
        suppress_explicit_web_intent=bool(
            use_turn_contract and capabilities and "search_browser" not in capabilities
        ),
    )
