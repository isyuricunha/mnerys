"""M2.1 admitted selection of the existing turn-contract and compact runtime.

The Web adapter still resolves intent and provider settings; this service only
chooses which inherited contract/runtime path applies. Neither a client-side
surface marker nor a selected mode grants tool authority. The existing tool
policy, immutable contract and hardened dispatcher continue to enforce it.
"""

from dataclasses import dataclass
from typing import Any

from src.agent_runtime.runtime_selection import uses_compact_preview_runtime
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context


@dataclass(frozen=True)
class AdmittedContractScope:
    """Eligibility decisions shared by the existing routing and model paths."""

    enabled: bool
    native_workspace_contract: bool


def turn_contract_enabled(
    *,
    exact_tool_approval: Any,
    runtime_surface: str,
    native_workspace_contract: bool,
    clean_v3_route: bool,
    full_schema_route: bool = False,
) -> bool:
    """Preserve the inherited compact/native versus full-schema rule.

    Full-schema models retain their existing complete permitted inventory.
    An exact approval continuation never creates a fresh capability contract.
    """
    return bool(
        exact_tool_approval is None
        and runtime_surface != "odysseus-tui"
        and not full_schema_route
        and (not native_workspace_contract or clean_v3_route)
    )


def _admitted_owner(
    admission: AdmittedSession,
    session: Any,
    context_owner: str | None,
) -> None:
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if owner != context_owner:
        raise AdmissionDenied(403, "Turn contract context owner does not match admission")


def select_admitted_compact_runtime(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    clean_route_requested: bool,
    full_schema_route: bool,
    exact_tool_approval: Any,
    runtime_surface: str,
    agent_mode: bool,
    agent_permitted: bool,
    image_generation: bool,
) -> bool:
    """Select compact preparation once before context building.

    Native workspace bypass is intentionally *not* consulted here. The
    preparation decision must not be vetoed by the later workspace policy.
    """
    _admitted_owner(admission, session, context_owner)
    for flag in (clean_route_requested, full_schema_route, agent_mode,
                 agent_permitted, image_generation):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted runtime-selection flag")
    if not isinstance(runtime_surface, str):
        raise AdmissionDenied(403, "Invalid trusted runtime surface")

    return uses_compact_preview_runtime(
        clean_route_requested=clean_route_requested,
        turn_contract_enabled=turn_contract_enabled(
            exact_tool_approval=exact_tool_approval,
            runtime_surface=runtime_surface,
            native_workspace_contract=False,
            clean_v3_route=clean_route_requested,
            full_schema_route=full_schema_route,
        ),
        agent_mode=agent_mode,
        agent_permitted=agent_permitted,
        image_generation=image_generation,
    )


def select_admitted_contract_scope(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    client_runtime_context: Any,
    workspace_agent_intent: bool,
    workspace_bound: bool,
    clean_route_requested: bool,
    full_schema_route: bool,
    exact_tool_approval: Any,
) -> AdmittedContractScope:
    """Preserve the late native-workspace/contract decision after intent routing.

    The runtime hint is not an authorization source: it is meaningful only
    with workspace intent or a server-bound workspace plus unattended mode.
    All actual tool grants remain filtered by server policy and dispatcher.
    """
    _admitted_owner(admission, session, context_owner)
    for flag in (workspace_agent_intent, workspace_bound, clean_route_requested,
                 full_schema_route):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted contract-selection flag")
    if client_runtime_context is not None and not isinstance(client_runtime_context, dict):
        raise AdmissionDenied(403, "Invalid parsed runtime context")

    context = client_runtime_context or {}
    surface = str(context.get("surface") or "")
    native_workspace_contract = bool(
        surface == "odysseus-native"
        and context.get("terminal_agent") is True
        and (
            workspace_agent_intent
            or (context.get("unattended_mode") is True and workspace_bound)
        )
    )
    return AdmittedContractScope(
        enabled=turn_contract_enabled(
            exact_tool_approval=exact_tool_approval,
            runtime_surface=surface,
            native_workspace_contract=native_workspace_contract,
            clean_v3_route=clean_route_requested,
            full_schema_route=full_schema_route,
        ),
        native_workspace_contract=native_workspace_contract,
    )
