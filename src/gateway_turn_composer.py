"""M2.1 direct, read-only text-turn composition using the existing Agent Core.

The trusted in-process composer proves a complete *execution* handoff:
issued session admission -> shared context ports -> server tool policy ->
bound RequestAuthority -> inherited Agent Loop. It deliberately disables
tools and does not persist either side of the conversation. These limits
avoid creating a second, incomplete session writer while Web-only approval,
attachment, tool inventory, response finalization and SSE delivery are being
migrated. It is NOT a remote client entry point or a Web drop-in replacement.
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
from src.gateway_agent_execution import stream_admitted_agent_turn
from src.tool_policy import known_tool_names
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
) -> AsyncGenerator[str | CoreNativeEvent, None]:
    """Run one non-persisting text-only Agent turn without any Web dependency.

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
    if type(native_terminal_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted terminal event mode")
    if (
        type(settings.max_rounds) is not int or not 1 <= settings.max_rounds <= 32
        or type(settings.max_tokens) is not int or not 1 <= settings.max_tokens <= 131072
        or not isinstance(settings.global_disabled_tools, tuple)
        or any(not isinstance(item, str) for item in settings.global_disabled_tools)
    ):
        raise AdmissionDenied(403, "Invalid trusted model limits")
    if ports is None or any(not callable(getattr(ports, name, None)) for name in _PORT_METHODS):
        raise AdmissionDenied(403, "Trusted chat context ports are required")

    # Reject an account that cannot use Agent before calling I/O-capable
    # preprocessing ports. Delegated-token restrictions remain enforced.
    limits = execution_limits_for_admission(
        admission, settings.privileges, browser_tools=frozenset(),
    )
    if not limits.allow_agent:
        raise AdmissionDenied(403, "Agent turns are not allowed for this account")

    # For this first direct vertical slice, tools are intentionally OFF.
    # A later cutover must build the full typed capability/contract policy
    # before it may activate any operation, rather than inferring grants here.
    final = finalize_admitted_turn_tool_policy(
        admission, session,
        context_owner=owner,
        base_disabled_tools=frozenset(known_tool_names()),
        privileges=settings.privileges,
        global_disabled_tools=list(settings.global_disabled_tools),
        browser_tools=frozenset(),
        last_user_message=request.message,
        chat_mode="agent",
        auto_escalated=False,
        workspace_agent_intent=False,
        use_turn_contract=False,
        allow_browser_for_web_turn=False,
        compare_mode=False,
        plan_mode=False,
    )
    policy = replace(
        final.policy,
        block_all_tool_calls=True,
        disable_mcp=True,
    )

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
        capabilities=(),
        internal_tool_request=False,
    )

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
        turn_contract=None,
        max_rounds=settings.max_rounds,
        max_tokens=settings.max_tokens,
        context_length=context.context_length,
        uploaded_files=[],
        native_terminal_events=native_terminal_events,
    )) as stream:
        async for chunk in stream:
            yield chunk
