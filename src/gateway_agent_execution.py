"""Owner-admitted Agent Loop execution through the existing TurnService.

This is the first callable in-process Gateway execution path: it accepts an
issued admission and server-loaded session, checks the final request authority
and effective tool policy, and invokes the *same* inherited Agent Loop through
TurnService. No HTTP Request or Web route dependency is involved.

It is not a public Gateway API, a new model executor, or durable turn storage.
In particular, a future remote transport must obtain admission, loaded
session, authority and policy from server-owned components; it must never
deserialize these trusted objects from a client's request body.
"""

from contextlib import aclosing
from typing import Any, AsyncGenerator

from src.agent_runtime.authority import RequestAuthority
from src.agent_core_events import CoreNativeEvent
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_execution_policy import execution_limits_for_admission
from src.tool_execution import AgentExecutionBridge
from src.tool_policy import ToolPolicy
from src.tool_approvals import ToolApprovalStore
from src.turn_service import TurnService


_PROTECTED_AGENT_FIELDS = frozenset({
    "session_id",
    "history_session",
    "owner",
    "headers",
    "workspace",
    "request_authority",
    "tool_policy",
    "turn_contract",
    "delegated_credential",
})


async def stream_admitted_agent_turn(
    admission: AdmittedSession,
    session: Any,
    turns: TurnService,
    *,
    context_owner: str | None,
    bridge: AgentExecutionBridge | None,
    endpoint_url: str,
    model: str,
    messages: list,
    authority: RequestAuthority,
    policy: ToolPolicy,
    workspace: str | None,
    turn_contract: Any,
    native_terminal_events: bool = False,
    native_tool_events: bool = False,
    native_route_events: bool = False,
    approval_store: ToolApprovalStore | None = None,
    **runtime_options: Any,
) -> AsyncGenerator[str | CoreNativeEvent, None]:
    """Execute the inherited Agent Loop only for a matching admitted turn.

    The caller must supply the server-selected final request authority/policy.
    The session ID, owner, model profile, provider headers and delegated flag
    are taken from the admitted loaded session, never arbitrary agent options.
    All other model/agent options preserve the original runtime's behavior.

    The existing dispatcher still decides whether each exact operation is
    authorized. This boundary does not mint grants or expose tools to callers.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Agent execution context owner does not match admission")
    if not isinstance(turns, TurnService):
        raise AdmissionDenied(403, "Invalid server agent runtime")
    if not isinstance(authority, RequestAuthority) or not isinstance(policy, ToolPolicy):
        raise AdmissionDenied(403, "Invalid server turn authority or policy")
    if bridge is not None and not isinstance(bridge, AgentExecutionBridge):
        raise AdmissionDenied(403, "Invalid server execution bridge")
    if (
        not isinstance(endpoint_url, str) or not endpoint_url.strip()
        or not isinstance(model, str) or not model.strip()
        or not isinstance(messages, list)
        or not (workspace is None or isinstance(workspace, str))
    ):
        raise AdmissionDenied(403, "Invalid selected agent model or request context")
    if (
        endpoint_url != getattr(session, "endpoint_url", None)
        or model != getattr(session, "model", None)
    ):
        raise AdmissionDenied(409, "Agent model does not match admitted session")

    # A request authority with a different owner, session or workspace must
    # never cross this boundary, even if the caller already has a valid session.
    if not authority.bound_to(
        owner=owner,
        session_id=session_id,
        workspace=workspace,
    ):
        raise AdmissionDenied(403, "Agent request authority does not match admission")
    if _PROTECTED_AGENT_FIELDS.intersection(runtime_options):
        raise AdmissionDenied(403, "Protected agent execution field cannot be overridden")
    if approval_store is not None and not isinstance(approval_store, ToolApprovalStore):
        raise AdmissionDenied(403, "Invalid trusted approval store")
    if type(native_tool_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted native tool routing flag")
    if type(native_route_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted native model route flag")
    if type(native_terminal_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted native terminal routing flag")
    if native_terminal_events and (
        bridge is not None
        or not policy.block_all_tool_calls
        or not policy.disable_mcp
    ):
        raise AdmissionDenied(403, "Native terminal events require a tools-disabled direct turn")

    requested_disabled = runtime_options.pop("disabled_tools", None)
    if requested_disabled is not None and (
        not isinstance(requested_disabled, (set, frozenset, list, tuple))
        or any(not isinstance(item, str) for item in requested_disabled)
    ):
        raise AdmissionDenied(403, "Invalid server tool denylist")

    # Even if a legacy caller omitted a denial while preparing agent options,
    # no tool blocked by the final policy or delegated admission is restored.
    limits = execution_limits_for_admission(
        admission, None, browser_tools=frozenset(),
    )
    disabled = (
        set(requested_disabled or ())
        | policy.all_disabled_names()
        | set(limits.disabled_tools)
    )
    effective_authority = authority.restrict(
        policy, disabled_tools=limits.disabled_tools,
    )

    options = dict(runtime_options)
    if approval_store is not None:
        options["approval_store"] = approval_store
    if native_tool_events:
        options["native_tool_events"] = True
    if native_route_events:
        options["native_route_events"] = True
    if native_terminal_events:
        # An explicit, admitted, no-tools Python consumer opts into Core
        # terminal objects; legacy Web receives the same original SSE bytes.
        options["native_terminal_events"] = True
    options.update({
        "headers": getattr(session, "headers", None),
        "session_id": session_id,
        "history_session": session,
        "owner": owner,
        "workspace": workspace or None,
        "tool_policy": policy,
        "request_authority": effective_authority,
        "turn_contract": turn_contract,
        "disabled_tools": disabled if disabled else None,
        "delegated_credential": admission.principal.delegated,
    })

    # The inner generator must close before bridge/contract contextvars reset
    # when the transport disconnects, cancels or errors mid-stream.
    async with aclosing(
        turns.stream_agent(
            bridge, endpoint_url, model, messages, **options,
        )
    ) as stream:
        async for chunk in stream:
            yield chunk
