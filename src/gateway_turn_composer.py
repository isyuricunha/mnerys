"""M2.1 direct text-input composition using the existing Agent Core.

One operation assembles shared context, owner-scoped policy, authority and,
for an explicitly server-offered native tool surface, the inherited inventory
and immutable routed contract. Tools remain disabled by default. Conversation
messages are never persisted here; enabled tools retain their own existing
authorization and effects. Native approval controls reuse the sealed store;
attachments, active UI resources and production Web cutover remain outside
this direct profile.
This is not a remote client entry point.
"""

from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
import json
from typing import Any, AsyncGenerator

from src.agent_core_events import CoreNativeEvent, completed_core_event
from src.gateway_approval_decisions import (
    ToolApprovalDecision, inspect_admitted_tool_approval, consume_admitted_tool_approval,
)
from src.tool_approvals import ExactToolApproval, ToolApprovalStore, tool_approval_store
from src.agent_runtime.resources import NativeBackendResource
from src.agent_runtime.authority import ExactOperation
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
from src.gateway_text_persistence import TrustedTextPersistence, require_trusted_text_persistence


@dataclass(frozen=True)
class TextTurnRequest:
    """Only untrusted user message text; no credentials, tool grants or paths."""

    message: str


@dataclass(frozen=True)
class ToolApprovalTurnRequest:
    """An untrusted control-plane choice, never a new user message/grant."""

    approval_id: str
    decision: str


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


def _approval_decision_frame(resolution: ToolApprovalDecision, native: bool) -> str | CoreNativeEvent:
    """Format an already-consumed fact; never expose the sealed operation."""
    if native:
        return resolution.event
    return "data: " + json.dumps({
        "type": "approval_decided", "approval_id": resolution.pending.approval_id,
        "decision": resolution.decision,
    }) + "\n\n"


async def stream_composed_text_turn(
    admission: AdmittedSession,
    session: Any,
    turns: TurnService,
    *,
    context_owner: str | None,
    request: TextTurnRequest | ToolApprovalTurnRequest,
    ports: ChatContextPorts,
    chat_handler: Any,
    chat_processor: Any,
    settings: TrustedTextTurnSettings,
    native_terminal_events: bool = False,
    native_tool_events: bool = False,
    approval_store: ToolApprovalStore | None = None,
    persistence: TrustedTextPersistence | None = None,
) -> AsyncGenerator[str | CoreNativeEvent, None]:
    """Compose a text-input Agent turn without conversation persistence.

    Production persistence and UI-bound approval continuations remain on the
    inherited Web path; native controls require a supported sealed context.
    This function must never be directly exposed to a remote caller.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Composer context owner does not match admission")
    if not isinstance(turns, TurnService):
        raise AdmissionDenied(403, "Invalid server agent runtime")
    continuation = isinstance(request, ToolApprovalTurnRequest)
    if continuation:
        if (
            type(request.approval_id) is not str or not request.approval_id
            or len(request.approval_id) > 256 or type(request.decision) is not str
        ):
            raise AdmissionDenied(403, "Invalid direct approval control")
    elif not isinstance(request, TextTurnRequest) or not isinstance(request.message, str) or not request.message.strip():
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
    if persistence is not None:
        require_trusted_text_persistence(persistence)
        if tools_enabled or continuation:
            raise AdmissionDenied(403, "Persisted direct turns require plain text without tools or approvals")
    if tools_enabled and native_terminal_events:
        raise AdmissionDenied(403, "Native terminal mode requires a tools-disabled turn")
    if ports is None or any(not callable(getattr(ports, name, None)) for name in _PORT_METHODS):
        raise AdmissionDenied(403, "Trusted chat context ports are required")

    store = tool_approval_store if approval_store is None else approval_store
    if not isinstance(store, ToolApprovalStore):
        raise AdmissionDenied(403, "Trusted approval store is required")
    pending = None
    decision = None
    if continuation:
        pending, decision = inspect_admitted_tool_approval(
            admission, request.approval_id, request.decision,
            plan_mode=False, internal_tool_request=False, store=store,
        )
        if decision == "deny":
            resolution = consume_admitted_tool_approval(
                admission, request.approval_id, decision,
                plan_mode=False, internal_tool_request=False, store=store,
            )
            yield _approval_decision_frame(resolution, native_tool_events)
            yield 'data: ' + json.dumps({"type": "final_response", "content": "The action was denied."}) + '\n\n'
            yield completed_core_event() if native_terminal_events else 'data: [DONE]\n\n'
            return
        # This direct profile has no workspace/active-document/browser adapter.
        # Retain unsupported cards rather than consuming a grant we cannot run.
        if (
            pending.tool_name not in settings.offered_tools
            or pending.workspace or pending.document_id or pending.document_version is not None
            or pending.document_digest or pending.resource_operation is not None
            or pending.process_operation is not None or pending.browser_operation is not None
            or pending.backend_operation is None
            or not isinstance(pending.backend_operation.resource, NativeBackendResource)
        ):
            raise AdmissionDenied(409, "Approval requires an unsupported direct-turn context")
        proof = ExactToolApproval(pending)
        if not proof.matches(owner=owner, session_id=session_id,
                             tool_name=pending.tool_name, content=pending.content, workspace=None):
            raise AdmissionDenied(409, "Approval action no longer matches its sealed record")
        message = pending.continuation_query or pending.request_text
        if not isinstance(message, str) or not message.strip():
            raise AdmissionDenied(409, "Approval has no originating turn context")
    else:
        message = request.message

    if any(type(value) is not str or not value.strip() for value in (
        getattr(session, "endpoint_url", None), getattr(session, "model", None),
    )):
        raise AdmissionDenied(403, "Invalid selected model for composed turn")

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
        last_user_message=message,
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
            message=message, plan_mode=False,
            native_workspace_contract=False,
        )
        inventory_names = frozenset(schema["function"]["name"] for schema in inventory.schemas)
        if not settings.offered_tools <= inventory_names:
            raise AdmissionDenied(403, "Adapter offered a tool outside the server inventory")
        # Deny every non-offered schema, including dynamic MCP aliases.
        denied = inventory_names - settings.offered_tools
        policy = build_effective_tool_policy(
            disabled_tools=(inventory.policy.all_disabled_names() | denied),
            last_user_message=message,
        )
        # Reuse the owner-bound services; schema selection is not authority.
        inventory = replace(inventory, policy=policy,
                            disabled_tools=frozenset(inventory.disabled_tools | denied))
        classified = classify_admitted_turn_capabilities(
            admission, session, context_owner=owner, message=message,
            use_turn_contract=True, active_document=False, workspace_bound=False,
            image_attachment=False, explicit_browser_intent=False,
            external_discovery_intent=False, private_browser_warm=False,
            contextual_browser_followup_lookup=lambda: False,
        )
        selection = select_admitted_tool_intent(
            admission, session, context_owner=owner, inventory=inventory,
            message=message, capabilities=classified.capabilities,
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

    if pending is not None:
        if policy.blocks(pending.tool_name):
            raise AdmissionDenied(403, "The approved tool is blocked by current policy")
        # Reuse the original authority snapshot, never derive grants from the
        # button label or the restored retrieval query.
        original_authority = authority_for_admitted_turn(
            admission, message, workspace=None, history=[], policy=policy,
            internal_tool_request=False, exact_approval=ExactToolApproval(pending),
        )
        try:
            operation = ExactOperation.normalize(pending.tool_name, pending.content)
        except (ValueError, TypeError) as exc:
            raise AdmissionDenied(409, "The sealed action has invalid arguments") from exc
        if original_authority.restricted(operation):
            raise AdmissionDenied(403, "The originating authority still blocks this action")
        selection = replace(
            selection,
            selected_tools=frozenset(pending.selected_tools) & settings.offered_tools,
            required_tools=frozenset({pending.tool_name}),
            active_capabilities=frozenset(),
        )
    retired_taint = False if continuation else store.retire_for_session(
        owner=owner, session_id=session_id,
    )

    # The real shared context algorithm is used unchanged with the injected
    # trusted ports. Do not append an orphaned user message while the response
    # finalization/persistence service is still owned by Web.
    context = await assemble_chat_context(
        session, owner, chat_handler, chat_processor,
        message=message,
        session_id=session_id,
        ports=ports,
        use_web=False,
        use_rag="false",
        no_memory=True,
        agent_mode=True,
        allow_tool_preprocessing=False,
        persist_user_message=persistence is not None,
        interaction_mode="agent" if persistence is not None else None,
        require_text_user_content=persistence is not None,
    )
    if not isinstance(context.messages, list):
        raise AdmissionDenied(403, "Invalid composed model context")
    # Context composition never writes conversation messages here. New user
    # input is appended only to the invocation; a resumed control restores
    # the originating context without appending the approval choice.
    if not isinstance(context.preprocessed.user_content, str):
        raise AdmissionDenied(403, "Direct composer supports text messages only")
    model_messages = list(context.messages)
    # Restore only the sealed originating input for a non-persisted turn.
    # An already stored copy is not appended again. The control choice is
    # never user text and never enters authority construction.
    if (persistence is None and not continuation) or (continuation and not any(
        isinstance(item, dict) and item.get("role") == "user"
        and item.get("content") == context.preprocessed.user_content
        for item in model_messages
    )):
        model_messages.append({"role": "user", "content": context.preprocessed.user_content})

    authority = authority_for_admitted_turn(
        admission, message,
        workspace=None,
        history=getattr(session, "history", []) or [],
        policy=policy,
        capabilities=capabilities,
        internal_tool_request=False,
        exact_approval=ExactToolApproval(pending) if pending is not None else None,
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
            message=message,
            history=getattr(session, "history", []) or [],
        )
        authority, contract = routed.authority, routed.contract
        if pending is not None:
            # The sealed first action is dispatched by the existing approval
            # branch. A lexical read requirement must not run it a second time.
            contract = replace(contract, required_read_operation=None)

    resolution = None
    if continuation:
        # All fallible context preparation completed. The store rechecks
        # expiry/owner/session and consumes exactly once, immediately before
        # yielding the decision and handing off to the existing Agent Loop.
        resolution = consume_admitted_tool_approval(
            admission, request.approval_id, decision,
            plan_mode=False, internal_tool_request=False, store=store,
        )
        if resolution.pending is not pending:
            raise AdmissionDenied(409, "Approval changed while preparing its continuation")
        yield _approval_decision_frame(resolution, native_tool_events)

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
        approval_store=store,
        **({
            "exact_approval": resolution.exact,
            "external_untrusted_context_seen": resolution.tainted,
            "relevant_tools": set(pending.selected_tools) & settings.offered_tools,
        } if resolution is not None else {"external_untrusted_context_seen": retired_taint}),
        native_terminal_events=native_terminal_events,
        native_tool_events=native_tool_events,
    )) as stream:
        async for chunk in stream:
            yield chunk
