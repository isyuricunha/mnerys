"""M2.1 admitted handoff to the existing immutable routed tool resolver.

This service does not invent an inventory or execute tools. It requires the
server-issued session admission and the existing request authority, delegates
selection to the single legacy resolver, and applies its tool-policy veto to
the same request authority before the runtime receives the result.
"""

from dataclasses import dataclass
from typing import Any, Iterable

from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)
from src.gateway_context_admission import owner_for_admitted_context
from src.tool_policy import ToolPolicy
from src.turn_contract import TurnContract, resolve_turn_contract


@dataclass(frozen=True)
class AdmittedRoutedContract:
    """The original immutable contract and policy-restricted turn authority."""

    contract: TurnContract
    authority: RequestAuthority


def resolve_admitted_routed_contract(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    request_authority: RequestAuthority,
    capabilities: Iterable[str],
    schemas: Iterable[dict],
    policy: ToolPolicy,
    required_tools: Iterable[str] = (),
    required_capabilities: Iterable[str] | None = None,
    selected_tools: Iterable[str] | None = None,
    always_available_tools: Iterable[str] = (),
    warm_tools: Iterable[str] = (),
    message: str | None = None,
    history: Iterable = (),
) -> AdmittedRoutedContract:
    """Delegate only an already-admitted owner's contract to the existing resolver.

    The HTTP adapter still selects request-specific tools and loads schemas.
    This service never treats an offered schema as permission to add a grant:
    request authority remains the independent execution-time bound.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if owner != context_owner:
        raise AdmissionDenied(403, "Routed contract context owner does not match admission")
    if not isinstance(policy, ToolPolicy) or not isinstance(request_authority, RequestAuthority):
        raise AdmissionDenied(403, "Trusted contract policy and request authority are required")
    if not request_authority.bound_to(
        owner=owner, session_id=session_id, workspace=request_authority.workspace,
    ):
        raise AdmissionDenied(403, "Routed contract authority does not match admitted session")

    contract = resolve_turn_contract(
        capabilities=capabilities,
        schemas=schemas,
        policy=policy,
        required_tools=required_tools,
        required_capabilities=required_capabilities,
        selected_tools=selected_tools,
        always_available_tools=always_available_tools,
        warm_tools=warm_tools,
        message=message,
        history=history,
    )
    return AdmittedRoutedContract(
        contract=contract,
        authority=request_authority.restrict(policy),
    )
