"""M2.1 direct text-input composition using the existing Agent Core.

One operation assembles shared context, owner-scoped policy, authority and,
for an explicitly server-offered native tool surface, the inherited inventory
and immutable routed contract. Tools remain disabled by default. Conversation
messages are never persisted here; enabled tools retain their own existing
authorization and effects. Attachments, active UI resources, approval
continuations and production Web cutover remain outside this direct profile.
This is not a remote client entry point.
"""

from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator

from src.agent_core_events import CoreNativeEvent
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_chat_context import assemble_chat_context
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_context_ports import ChatContextPorts
from src.gateway_execution_policy import (
    execution_limits_for_admission, finalize_admitted_turn_tool_policy,
)
from src.gateway_turn_authority import authority_for_admitted_turn
from src.gateway_tool_inventory import prepare_admitted_tool_inventory
from src.gateway_turn_capabilities import classify_admitted_turn_capabilities
from src.gateway_tool_intent import select_admitted_tool_intent
from src.gateway_routed_contract import resolve_admitted_routed_contract
from src.gateway_agent_execution import stream_admitted_agent_turn
from src.tool_policy import known_tool_names, build_effective_tool_policy
from src.turn_service import TurnService


@dataclass(frozen=True)
class TextTurnRequest:
    """Only untrusted user message text; no credentials, tool grants or paths."""

    message: str


@dataclass(frozen=True)
class TrustedTextTurnSettings:
    """Server-selected settings; never deserialize from a client request."""

    privileges: Mapping[str, Any] | None = None
    global_disabled_tools: tuple[str, ...] = ()
    max_rounds: int = 2
    max_tokens: int = 1024
    # Adapter-supported schema names, selected by the server. This only
    # narrows the inherited inventory; request authority still gates effects.
    offered_tools: frozenset[str] = frozenset()


_PORT_METHODS = (
    "extract_preset", "preprocess_message", "append_user_message",
    "append_private_message", "private_messages", "load_preferences",
    "upload_manifest", "build_preface", "used_memories", "compact", "trim",
    "youtube_sources", "normalize_model", "session_history",
    "current_datetime_message", "context_length", "estimate_tokens",
)


async def stream_composed_text_turn(
    admission: AdmittedSession,
    session: Any,
    turns: TurnService,
    *,
    context_owner: str | None,
    request: TextTurnRequest,
    ports: ChatContextPorts,
    chat_handler: Any,
    chat_processor: Any,
    settings: TrustedTextTurnSettings,
    native_terminal_events: bool = False,
    native_tool_events: bool = False,
) -> AsyncGenerator[str | CoreNativeEvent, None]:
    """Compose a text-input Agent turn without conversation persistence.

    Production turn persistence and approval continuity remain on the
    inherited Web path until the full M2.1 composer is acceptance-tested.
    This function must never be directly exposed to a remote caller.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Composer context owner does not match admission")
    if not isinstance(turns, TurnService):
        raise AdmissionDenied(403, "Invalid server agent runtime")
    if not isinstance(request, TextTurnRequest) or not isinstance(request.message, str) or not request.message.strip():
        raise AdmissionDenied(403, "Invalid direct text turn")
    if not isinstance(settings, TrustedTextTurnSettings):
        raise AdmissionDenied(403, "Invalid trusted turn settings")
    if type(native_tool_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted tool event mode")
    if type(native_terminal_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted terminal event mode")
    if (
        type(settings.max_rounds) is not int or not 1 <= settings.max_rounds <= 32
        or type(settings.max_tokens) is not int or not 1 <= settings.max_tokens <= 131072
        or not isinstance(settings.global_disabled_tools, tuple)
        or any(not isinstance(item, str) for item in settings.global_disabled_tools)
    ):
        raise AdmissionDenied(403, "Invalid trusted model limits")
    if (
        not isinstance(settings.offered_tools, frozenset)
        or any(type(name) is not str or not name or name.startswith("mcp__")
               for name in settings.offered_tools)
    ):
        raise AdmissionDenied(403, "Invalid trusted adapter tool surface")
    tools_enabled = bool(settings.offered_tools)
    if tools_enabled and native_terminal_events:
        raise AdmissionDenied(403, "Native terminal mode requires a tools-disabled turn")
    if ports is None or any(not callable(getattr(ports, name, None)) for name in _PORT_METHODS):
        raise AdmissionDenied(403, "Trusted chat context ports are required")

    # Reject an account that cannot use Agent before calling I/O-capable
    # preprocessing ports. Delegated-token restrictions remain enforced.
    limits = execution_limits_for_admission(
        admission, settings.privileges, browser_tools=frozenset(),
    )
    if not limits.allow_agent:
        raise AdmissionDenied(403, "Agent turns are not allowed for this account")

    # The server's adapter surface can only remove tools. Build every
    # subsequent selection from the same original policy and dispatcher.
    final = finalize_admitted_turn_tool_policy(
        admission, session,
        context_owner=owner,
        base_disabled_tools=frozenset(known_tool_names()) - settings.offered_tools,
        privileges=settings.privileges,
        global_disabled_tools=list(settings.global_disabled_tools),
        browser_tools=frozenset(),
        last_user_message=request.message,
        chat_mode="agent",
        auto_escalated=False,
        workspace_agent_intent=False,
        use_turn_contract=tools_enabled,
        allow_browser_for_web_turn=False,
        compare_mode=False,
        plan_mode=False,
    )
    policy = final.policy
    capabilities = frozenset()
    contract = None
    selection = None
    inventory = None
    if tools_enabled:
        inventory = prepare_admitted_tool_inventory(
            admission, session, context_owner=owner,
            base_policy=policy, base_disabled_tools=policy.all_disabled_names(),
            message=request.message, plan_mode=False,
            native_workspace_contract=False,
        )
        inventory_names = frozenset(schema["function"]["name"] for schema in inventory.schemas)
        if not settings.offered_tools <= inventory_names:
            raise AdmissionDenied(403, "Adapter offered a tool outside the server inventory")
        # Deny every non-offered schema, including dynamic MCP aliases.
        denied = inventory_names - settings.offered_tools
        policy = build_effective_tool_policy(
            disabled_tools=(inventory.policy.all_disabled_names() | denied),
            last_user_message=request.message,
        )
        # Reuse the owner-bound services; schema selection is not authority.
        inventory = replace(inventory, policy=policy,
                            disabled_tools=frozenset(inventory.disabled_tools | denied))
        classified = classify_admitted_turn_capabilities(
            admission, session, context_owner=owner, message=request.message,
            use_turn_contract=True, active_document=False, workspace_bound=False,
            image_attachment=False, explicit_browser_intent=False,
            external_discovery_intent=False, private_browser_warm=False,
            contextual_browser_followup_lookup=lambda: False,
        )
        selection = select_admitted_tool_intent(
            admission, session, context_owner=owner, inventory=inventory,
            message=request.message, capabilities=classified.capabilities,
            active_capabilities=classified.active_capabilities,
            warm_tools=frozenset(), recent_web_tool_lookup=lambda: None,
            active_document=False, contextual_browser_followup=False,
            contextual_web_followup=False, active_email_review=False,
            corrected_browser_target=False,
        )
        # This first composed tool profile is native-only. Independent MCP
        # continuation and adapter-specific permissions remain future work.
        policy = replace(selection.policy, disable_mcp=True)
        capabilities = selection.capabilities
    else:
        policy = replace(policy, block_all_tool_calls=True, disable_mcp=True)

    # The real shared context algorithm is used unchanged with the injected
    # trusted ports. Do not append an orphaned user message while the response
    # finalization/persistence service is still owned by Web.
    context = await assemble_chat_context(
        session, owner, chat_handler, chat_processor,
        message=request.message,
        session_id=session_id,
        ports=ports,
        use_web=False,
        use_rag="false",
        no_memory=True,
        agent_mode=True,
        allow_tool_preprocessing=False,
        persist_user_message=False,
    )
    if not isinstance(context.messages, list):
        raise AdmissionDenied(403, "Invalid composed model context")
    # In the inherited context builder, persist_user_message=False is used
    # for approval continuations whose user input is already in history.
    # This direct, intentionally non-persisting path is a *new* turn: append
    # the trusted preprocessed text to this invocation only, not to storage.
    if not isinstance(context.preprocessed.user_content, str):
        raise AdmissionDenied(403, "Direct composer supports text messages only")
    model_messages = [*context.messages, {
        "role": "user", "content": context.preprocessed.user_content,
    }]

    authority = authority_for_admitted_turn(
        admission, request.message,
        workspace=None,
        history=getattr(session, "history", []) or [],
        policy=policy,
        capabilities=capabilities,
        internal_tool_request=False,
    )

    if tools_enabled:
        routed = resolve_admitted_routed_contract(
            admission, session, context_owner=owner,
            request_authority=authority, capabilities=capabilities,
            schemas=inventory.schemas, policy=policy,
            required_tools=selection.required_tools,
            required_capabilities=selection.active_capabilities,
            selected_tools=selection.selected_tools,
            warm_tools=selection.warm_tools,
            message=request.message,
            history=getattr(session, "history", []) or [],
        )
        authority, contract = routed.authority, routed.contract

    async with aclosing(stream_admitted_agent_turn(
        admission, session, turns,
        context_owner=owner,
        bridge=None,
        endpoint_url=session.endpoint_url,
        model=session.model,
        messages=model_messages,
        authority=authority,
        policy=policy,
        workspace=None,
        turn_contract=contract,
        max_rounds=settings.max_rounds,
        max_tokens=settings.max_tokens,
        context_length=context.context_length,
        uploaded_files=[],
        native_terminal_events=native_terminal_events,
        native_tool_events=native_tool_events,
    )) as stream:
        async for chunk in stream:
            yield chunk
