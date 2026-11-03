"""M2.1 admitted final compact scope and tool-denial reconciliation.

This service reuses the original immutable contract, compact preview scoper,
optional account/model-scoped experiment and existing policy builder. It does
not create tool grants or run an agent or tool. The HTTP adapter still prepares
server-resolved inventory, flags and current-turn selected operations.
"""
from dataclasses import dataclass, replace
from typing import Any, Iterable

from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_routed_contract import AdmittedRoutedContract
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.turn_contract import TurnContract, canonical_tool, resolve_full_inventory_contract


@dataclass(frozen=True)
class AdmittedScopedContract:
    """Final immutable contract, original bounded authority, and runtime policy."""

    contract: TurnContract
    authority: RequestAuthority
    disabled_tools: frozenset[str]
    policy: ToolPolicy


def scope_admitted_routed_contract(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    routed: AdmittedRoutedContract,
    schemas: Iterable[dict],
    contract_policy: ToolPolicy,
    disabled_tools: set[str] | frozenset[str],
    active_capabilities: Iterable[str],
    selected_tools: Iterable[str] | None,
    warm_tools: Iterable[str],
    compact_preview: bool,
    native_workspace_contract: bool,
    structured_document_intent: bool,
    local_browser_render_intent: bool,
    experiment_header: str | None,
    message: str,
    history: Iterable,
    browser_requested: bool,
) -> AdmittedScopedContract:
    """Scope only an admitted contract, then reconcile the runtime denylist.

    The route supplies the trusted policy-filtered schema inventory and flags.
    The experiment selector checks the admitted owner plus the session's model,
    never a user-supplied owner/mode. Any offered inventory remains subject
    to contract_policy; no offered schema is a fresh execution grant.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Scoped contract context owner does not match admission")
    if (
        not isinstance(routed, AdmittedRoutedContract)
        or not isinstance(routed.contract, TurnContract)
        or not isinstance(routed.authority, RequestAuthority)
        or not routed.authority.bound_to(
            owner=owner, session_id=session_id,
            workspace=routed.authority.workspace,
        )
    ):
        raise AdmissionDenied(403, "Trusted admitted routed contract is required")
    if not isinstance(contract_policy, ToolPolicy) or not isinstance(disabled_tools, (set, frozenset)):
        raise AdmissionDenied(403, "Invalid scoped contract policy")
    for flag in (
        compact_preview, native_workspace_contract, structured_document_intent,
        local_browser_render_intent, browser_requested,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted contract scoping flag")
    if experiment_header is not None and not isinstance(experiment_header, str):
        raise AdmissionDenied(403, "Invalid routing experiment header")
    if not isinstance(message, str):
        raise AdmissionDenied(403, "Invalid routed request text")

    # An internal caller must not supply a policy-incompatible contract or
    # an older, less-restricted authority. The previous admitted resolver
    # already guarantees both; repeat these monotonic checks at this boundary.
    denied = {canonical_tool(name) for name in contract_policy.all_disabled_names()}
    if (
        not denied <= routed.authority.denied
        or (contract_policy.block_all_tool_calls and not routed.authority.block_all)
        or (contract_policy.disable_mcp and not routed.authority.disable_mcp)
        or any(
            contract_policy.blocks(name)
            or canonical_tool(name) in denied
            or (contract_policy.disable_mcp and name.startswith("mcp__"))
            for name in routed.contract.offered
        )
    ):
        raise AdmissionDenied(403, "Routed contract exceeds admitted tool policy")

    # A stale pre-routing disabled snapshot must not veto a tool admitted by
    # the policy-filtered immutable resolver. The later compact scope can
    # still narrow that offered set; its omissions become explicit denials.
    disabled = set(disabled_tools)
    routed_contract = routed.contract
    disabled.difference_update(routed_contract.offered)
    contract = routed_contract
    schema_list = list(schemas)

    if compact_preview:
        # Optional compact/runtime imports stay out of the default Gateway
        # module import graph, as in the original Web-only branch.
        from src.clean_agent_preview import (
            INTERACTIVE_CORE_TOOLS, MODE, NATIVE_WORKSPACE_TOOLS,
            PREVIEW_TOOLS, canonical, scope_preview_contract,
        )
        from src.tool_routing_experiment import (
            experiment_mode, select_experiment_inventory,
        )

        warm_canonical = {canonical(name) for name in warm_tools}
        clean_runtime_tools = PREVIEW_TOOLS | (
            NATIVE_WORKSPACE_TOOLS if native_workspace_contract else frozenset()
        )
        preview_schemas = [
            schema for schema in schema_list
            if canonical(schema["function"]["name"]) in clean_runtime_tools
        ]
        if native_workspace_contract and structured_document_intent:
            # Prefer structured readers for source discovery; allow a previously
            # successful shell tool only when its typed warm state is present.
            preview_schemas = [
                schema for schema in preview_schemas
                if canonical(schema["function"]["name"]) != "bash"
                or canonical(schema["function"]["name"]) in warm_canonical
            ]

        exact_native_chain = bool(
            selected_tools
            and {"write_file", "read_file"}.issubset(selected_tools)
            and set(selected_tools).intersection({"inspect_media", "extract_text"})
            and set(selected_tools).issubset(
                {"inspect_media", "extract_text", "write_file", "read_file"}
            )
        )
        active = frozenset(active_capabilities)
        extra_tools = (
            frozenset()
            if exact_native_chain or active == frozenset({"tasks"}) or (
                active in (frozenset({"transcription"}), frozenset({"ocr"}))
                and not selected_tools
            )
            else INTERACTIVE_CORE_TOOLS | warm_tools | (
                NATIVE_WORKSPACE_TOOLS | (
                    {"private_browser"} if local_browser_render_intent else frozenset()
                )
                if native_workspace_contract else frozenset()
            )
        )
        contract = scope_preview_contract(
            replace(
                resolve_full_inventory_contract(
                    schemas=preview_schemas, policy=contract_policy,
                ),
                selection_mode=MODE,
            ),
            routed_contract, active_capabilities, extra_tools=extra_tools,
        )
        # Only approved fixture/model owners may use a non-baseline experiment.
        # Resolve the header within the Gateway against the *admitted* owner.
        selected_experiment = experiment_mode(
            experiment_header, owner, model=getattr(session, "model", ""),
        )
        if selected_experiment != "baseline":
            contract = select_experiment_inventory(
                replace(
                    resolve_full_inventory_contract(
                        schemas=[
                            schema for schema in schema_list
                            if canonical(schema["function"]["name"]) in clean_runtime_tools
                        ],
                        policy=contract_policy,
                    ),
                    selection_mode=MODE,
                ),
                routed_contract, history, selected_experiment,
                user_text=message, browser_requested=browser_requested,
            )

    # Keep original post-scope ordering. A final contract cannot restore a
    # globally/account-denied name because the inventory already passed
    # contract_policy. The agent's hardened dispatcher also enforces authority.
    disabled.update(
        schema["function"]["name"] for schema in schema_list
        if not contract.permits(schema["function"]["name"])
    )
    disabled.difference_update(contract.offered)
    final_policy = build_effective_tool_policy(
        disabled_tools=disabled, last_user_message=message,
    )
    return AdmittedScopedContract(
        contract=contract, authority=routed.authority,
        disabled_tools=frozenset(disabled), policy=final_policy,
    )
