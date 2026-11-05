"""M2.1 direct text-input composition using the existing Agent Core.

One operation assembles shared context, owner-scoped policy, authority and,
for an explicitly server-offered native tool surface, the inherited inventory
and immutable routed contract. Tools remain disabled by default. An explicitly
server-selected session workspace can accompany native-only tools and sealed
native approvals; filesystem actions require the original sealed resource
snapshot and matching admitted workspace. Unbound effects remain unsupported.
Conversation persistence is separately opt-in for tools-disabled text turns.
The Web production cutover remains outside this direct profile.
This is not a remote client entry point.
"""

from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
import json
import logging
import os
from typing import Any, AsyncGenerator

from src.agent_core_events import CoreNativeEvent, completed_core_event
from src.gateway_approval_decisions import (
    ToolApprovalDecision, inspect_admitted_tool_approval, consume_admitted_tool_approval,
)
from src.tool_approvals import (
    ExactToolApproval, ToolApprovalStore, tool_approval_store,
    _normalized_workspace,
)
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
from src.gateway_attachment_context import (
    validate_text_attachment_ids, resolve_admitted_text_attachments,
)
from src.prompt_security import untrusted_context_message
from src.gateway_foreground_routes import (
    resolve_admitted_foreground_policy, build_admitted_foreground_routes,
)
from src.tool_policy import known_tool_names, build_effective_tool_policy
from src.turn_service import TurnService
from src.gateway_filesystem_context import require_sealed_filesystem_approval
from src.agent_runtime.resource_binding import NATIVE_FILESYSTEM_TOOLS
from src.gateway_document_context import (
    load_admitted_active_document, require_sealed_document_snapshot,
)
from src.gateway_text_persistence import (
    TrustedTextPersistence, require_trusted_text_persistence,
    snapshot_session_receipts, require_new_session_receipt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextTurnRequest:
    """Only untrusted user message text; no credentials, tool grants or paths."""

    message: str
    # Untrusted ID selectors; resolved by the owner-scoped server upload port.
    attachment_ids: tuple[str, ...] = ()


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
    # Trusted adapter setting, checked against the loaded session cwd.
    # Never deserialize a client-supplied workspace into this field.
    workspace: str | None = None
    # Explicit server-selected active editor context. The selected ID is
    # always owner/session-verified against the existing SQL document store.
    active_document_id: str | None = None
    # Opt-in: the trusted server adapter supplies the allowed model names;
    # the user must separately enable the owner-scoped foreground preference.
    enable_model_fallback: bool = False
    allowed_fallback_models: frozenset[str] = frozenset()
    # The trusted Web adapter may preserve the original plain-chat history
    # marker. Default direct Agent behavior remains unchanged.
    interaction_mode: str = "agent"
    # The HTTP Web adapter alone may persist a verified text-only upload
    # after a second owner/hash read, without storing the upload as user prose.
    persist_verified_text_attachments: bool = False


def _admitted_session_workspace(session: Any, configured: str | None) -> str | None:
    """Allow only an existing workspace already bound to the loaded session.

    This is not a filesystem grant or a replacement for the dispatcher's
    resource-confinement checks. A full Web workspace resolver is still needed.
    """
    if configured is None:
        return None
    stored = getattr(session, "cwd", None)
    if (
        type(configured) is not str or type(stored) is not str
        or not configured or not stored
        or len(configured) > 4096 or len(stored) > 4096
        or any(ord(char) < 32 for char in configured + stored)
        or not os.path.isabs(configured) or not os.path.isabs(stored)
    ):
        raise AdmissionDenied(403, "Invalid trusted session workspace")
    try:
        chosen = _normalized_workspace(configured)
        stored_path = _normalized_workspace(stored)
        if chosen != stored_path or not os.path.isdir(chosen):
            raise AdmissionDenied(403, "Workspace differs from the admitted session")
    except (OSError, ValueError) as exc:
        raise AdmissionDenied(403, "Invalid trusted session workspace") from exc
    return chosen


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
    native_route_events: bool = False,
    approval_store: ToolApprovalStore | None = None,
    persistence: TrustedTextPersistence | None = None,
) -> AsyncGenerator[str | CoreNativeEvent, None]:
    """Compose one admitted text-input turn with existing Agent Core services.

    Native-only approvals may resume in the admitted session workspace.
    Full Web/attachment/remote resource continuation remains inherited.
    This function must never be directly exposed to a remote caller.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Composer context owner does not match admission")
    if not isinstance(turns, TurnService):
        raise AdmissionDenied(403, "Invalid server agent runtime")
    continuation = isinstance(request, ToolApprovalTurnRequest)
    attachment_ids = ()
    if continuation:
        if (
            type(request.approval_id) is not str or not request.approval_id
            or len(request.approval_id) > 256 or type(request.decision) is not str
        ):
            raise AdmissionDenied(403, "Invalid direct approval control")
    elif not isinstance(request, TextTurnRequest) or not isinstance(request.message, str) or not request.message.strip():
        raise AdmissionDenied(403, "Invalid direct text turn")
    else:
        attachment_ids = validate_text_attachment_ids(request.attachment_ids)
    if not isinstance(settings, TrustedTextTurnSettings):
        raise AdmissionDenied(403, "Invalid trusted turn settings")
    if (
        type(settings.interaction_mode) is not str
        or settings.interaction_mode not in {"chat", "agent"}
        or (settings.interaction_mode == "chat" and
            (settings.offered_tools or continuation))
    ):
        raise AdmissionDenied(403, "Unsupported trusted turn interaction mode")
    if type(native_tool_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted tool event mode")
    if type(native_route_events) is not bool:
        raise AdmissionDenied(403, "Invalid trusted model route observation mode")
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
    if (
        type(settings.enable_model_fallback) is not bool
        or not isinstance(settings.allowed_fallback_models, frozenset)
        or len(settings.allowed_fallback_models) > 128
        or any(
            type(item) is not str or not item.strip() or len(item) > 256
            for item in settings.allowed_fallback_models
        )
        or (
            settings.enable_model_fallback
            and not settings.allowed_fallback_models
        )
    ):
        raise AdmissionDenied(403, "Invalid trusted fallback model allowance")
    tools_enabled = bool(settings.offered_tools)
    persist_uploaded_text = bool(attachment_ids and persistence is not None)
    if (
        type(settings.persist_verified_text_attachments) is not bool
        or (settings.persist_verified_text_attachments and (
            not persist_uploaded_text or settings.interaction_mode != "chat"
        ))
        or (persist_uploaded_text and not settings.persist_verified_text_attachments)
    ):
        raise AdmissionDenied(403, "Unsupported persisted attachment profile")
    if attachment_ids and (
        tools_enabled or settings.workspace is not None
        or settings.active_document_id is not None
    ):
        # Text upload context is read-only; it cannot grant file or tool IO.
        raise AdmissionDenied(403, "Text attachments require a read-only turn")
    if persistence is not None:
        require_trusted_text_persistence(persistence)
        if (tools_enabled or continuation or settings.workspace is not None
                or settings.active_document_id is not None):
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
    document_bound = False
    filesystem_bound = False
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

    # A deny choice is always safe to close. Any attempt to run or read a
    # document/workspace must pass account restrictions before filesystem or
    # database I/O, including when the client resumes a sealed approval.
    limits = execution_limits_for_admission(
        admission, settings.privileges, browser_tools=frozenset(),
    )
    if not limits.allow_agent:
        raise AdmissionDenied(403, "Agent turns are not allowed for this account")

    if continuation:
        workspace = _admitted_session_workspace(session, settings.workspace)
        # Only sealed owned-document edits are admitted; browser/filesystem
        # approval resources remain unsupported.
        document_bound = bool(
            pending.document_id or pending.document_digest
            or pending.document_version is not None
        )
        filesystem_bound = pending.tool_name in NATIVE_FILESYSTEM_TOOLS
        if document_bound:
            if pending.tool_name not in {
                "edit_document", "update_document", "suggest_document",
            }:
                raise AdmissionDenied(409, "Document approval uses an unsupported operation")
        elif pending.tool_name in {
            "edit_document", "update_document", "suggest_document",
        }:
            raise AdmissionDenied(409, "Document approval has no sealed resource")
        # Retain unsupported cards rather than consuming a grant we cannot run.
        if (
            pending.tool_name not in settings.offered_tools
            or pending.workspace != (workspace or "")
            or (pending.resource_operation is not None and not filesystem_bound)
            or pending.process_operation is not None or pending.browser_operation is not None
            or pending.backend_operation is None
            or not isinstance(pending.backend_operation.resource, NativeBackendResource)
        ):
            raise AdmissionDenied(409, "Approval requires an unsupported direct-turn context")
        proof = ExactToolApproval(pending)
        if not proof.matches(owner=owner, session_id=session_id,
                             tool_name=pending.tool_name, content=pending.content, workspace=workspace):
            raise AdmissionDenied(409, "Approval action no longer matches its sealed record")
        message = pending.continuation_query or pending.request_text
        if not isinstance(message, str) or not message.strip():
            raise AdmissionDenied(409, "Approval has no originating turn context")
    else:
        workspace = _admitted_session_workspace(session, settings.workspace)
        message = request.message

    if any(type(value) is not str or not value.strip() for value in (
        getattr(session, "endpoint_url", None), getattr(session, "model", None),
    )):
        raise AdmissionDenied(403, "Invalid selected model for composed turn")

    # Reading documents is I/O. Admission and account permission must be
    # settled first; the pending card is never consumed by this lookup.
    active_document = (
        load_admitted_active_document(
            owner=owner, session_id=session_id,
            document_id=settings.active_document_id,
        ) if settings.active_document_id is not None else None
    )
    if document_bound:
        require_sealed_document_snapshot(pending, active_document)

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
        workspace_agent_intent=bool(workspace),
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
            use_turn_contract=True, active_document=active_document is not None,
            workspace_bound=bool(workspace),
            image_attachment=False, explicit_browser_intent=False,
            external_discovery_intent=False, private_browser_warm=False,
            contextual_browser_followup_lookup=lambda: False,
        )
        selection = select_admitted_tool_intent(
            admission, session, context_owner=owner, inventory=inventory,
            message=message, capabilities=classified.capabilities,
            active_capabilities=classified.active_capabilities,
            warm_tools=frozenset(), recent_web_tool_lookup=lambda: None,
            active_document=active_document is not None,
            contextual_browser_followup=False,
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
        if filesystem_bound:
            require_sealed_filesystem_approval(
                pending, owner=owner, session_id=session_id, workspace=workspace,
            )
            # The restored continuation query may be lexically "unknown".
            # Route only the already sealed filesystem tool family, narrowed
            # by selected_tools, policy and exact request authority below.
            # This is selection, never a new request grant.
            capabilities = frozenset((*capabilities, "shell_files"))
        # Reuse the original authority snapshot, never derive grants from the
        # button label or the restored retrieval query.
        original_authority = authority_for_admitted_turn(
            admission, message, workspace=workspace, history=[], policy=policy,
            internal_tool_request=False, exact_approval=ExactToolApproval(pending),
        )
        try:
            operation = ExactOperation.normalize(pending.tool_name, pending.content)
        except (ValueError, TypeError) as exc:
            raise AdmissionDenied(409, "The sealed action has invalid arguments") from exc
        if original_authority.restricted(operation):
            raise AdmissionDenied(403, "The originating authority still blocks this action")
        # A filesystem continuation stays restricted to the one approved
        # native tool class. Do not widen the route to shell/process or other
        # tools merely because the originating inventory also offered them.
        restored_tools = (
            frozenset({pending.tool_name}) if filesystem_bound
            else frozenset(pending.selected_tools)
        )
        selection = replace(
            selection,
            selected_tools=restored_tools & settings.offered_tools,
            required_tools=frozenset({pending.tool_name}),
            active_capabilities=frozenset(),
        )
    retired_taint = False if continuation else store.retire_for_session(
        owner=owner, session_id=session_id,
    )

    # Resolve owner-bound uploaded files before preprocessing any user content.
    # The server upload manifest is checked again after async processing to
    # reject missing, substituted or rewritten file snapshots.
    upload_handler = getattr(chat_handler, "upload_handler", None)
    attachments = resolve_admitted_text_attachments(
        attachment_ids=attachment_ids, owner=owner,
        upload_handler=upload_handler, ports=ports,
    )

    # The real shared context algorithm is used unchanged with the injected
    # trusted ports. Do not append an orphaned user message while the response
    # finalization/persistence service is still owned by Web.
    user_write_cursor = snapshot_session_receipts(session) if persistence is not None else None

    def on_confirmed_user_append() -> None:
        # Never fire user events on an orphaned or failed legacy append.
        require_new_session_receipt(session, user_write_cursor, "user")
        if persistence is not None and persistence.after_user_saved is not None:
            try:
                persistence.after_user_saved()
            except Exception:
                # A notification failed after the user DB write; do not
                # reinterpret that committed message as an unpersisted input.
                logger.warning("Admitted user-message notification failed after save")

    context = await assemble_chat_context(
        session, owner, chat_handler, chat_processor,
        message=message,
        session_id=session_id,
        att_ids=list(attachment_ids),
        ports=ports,
        continuation_context_message=message if attachment_ids else None,
        allow_attachment_preprocessing=bool(attachment_ids),
        use_web=False,
        use_rag="false",
        no_memory=True,
        agent_mode=settings.interaction_mode == "agent",
        allow_tool_preprocessing=False,
        # Validate uploaded text *after* asynchronous preprocessing and
        # before any irreversible user write; ordinary plain text is unchanged.
        persist_user_message=persistence is not None and not attachment_ids,
        interaction_mode=settings.interaction_mode if persistence is not None else None,
        require_text_user_content=persistence is not None,
        on_user_message_persisted=(
            on_confirmed_user_append if user_write_cursor is not None else None
        ),
    )
    if not isinstance(context.messages, list):
        raise AdmissionDenied(403, "Invalid composed model context")
    # Context composition never writes conversation messages here. New user
    # input is appended only to the invocation; a resumed control restores
    # the originating context without appending the approval choice.
    if not isinstance(context.preprocessed.user_content, str):
        raise AdmissionDenied(403, "Direct composer supports text messages only")
    attachment_text = None
    if attachment_ids:
        if (
            resolve_admitted_text_attachments(
                attachment_ids=attachment_ids, owner=owner,
                upload_handler=upload_handler, ports=ports,
            ) != attachments
            or context.auto_opened_docs
            or len(context.preprocessed.attachment_meta) != len(attachment_ids)
            or tuple(item.get("id") for item in context.preprocessed.attachment_meta
                     if isinstance(item, dict)) != attachment_ids
            or len(context.uploaded_files) != len(attachment_ids)
            or tuple(item.get("id") for item in context.uploaded_files
                     if isinstance(item, dict)) != attachment_ids
            or not context.preprocessed.user_content.startswith(message)
        ):
            raise AdmissionDenied(409, "Selected text attachment changed during preparation")
        attachment_text = context.preprocessed.user_content[len(message):].strip()
        if not attachment_text or len(attachment_text) > 65536:
            raise AdmissionDenied(409, "Unsupported direct attachment content")

        if persist_uploaded_text:
            # Revalidate admission and seal first. Persist only the human
            # request and owner-checked attachment *references*, not the
            # untrusted file body as a future trusted user instruction.
            owner_for_admitted_context(admission, session, session_id)
            user_write_cursor = snapshot_session_receipts(session)
            safe_user = replace(
                context.preprocessed,
                enhanced_message=message,
                user_content=message,
                text_for_context=message,
            )
            ports.append_user_message(
                session, chat_handler, safe_user,
                incognito=False, interaction_mode="chat", auto_escalated=False,
            )
            on_confirmed_user_append()

    model_messages = list(context.messages)
    # Restore only the sealed originating input for a non-persisted turn.
    # An already stored copy is not appended again. The control choice is
    # never user text and never enters authority construction.
    if attachment_text is not None:
        # Content extracted from an upload is data, never the user's request.
        # The existing preface sees only the original request, not the file.
        model_messages.append(untrusted_context_message(
            "current user-selected text attachments", attachment_text,
        ))
        model_messages.append({"role": "user", "content": message})
    elif (persistence is None and not continuation) or (continuation and not any(
        isinstance(item, dict) and item.get("role") == "user"
        and item.get("content") == context.preprocessed.user_content
        for item in model_messages
    )):
        model_messages.append({"role": "user", "content": context.preprocessed.user_content})

    authority = authority_for_admitted_turn(
        admission, message,
        workspace=workspace,
        history=getattr(session, "history", []) or [],
        policy=policy,
        capabilities=capabilities,
        active_document=active_document is not None,
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

    # The context ports are asynchronous, so recheck the SQL document and
    # sealed resource identity before a one-use approval is consumed or a
    # selected active document is passed into the model.
    if active_document is not None:
        current_document = load_admitted_active_document(
            owner=owner, session_id=session_id,
            document_id=active_document.id,
        )
        if current_document != active_document:
            raise AdmissionDenied(409, "Active document changed during preparation")
        if pending is not None and pending.document_id:
            require_sealed_document_snapshot(pending, current_document)

    # The selected model remains the owner-admitted session model. Optional
    # fallback candidates and their credential-bearing headers are resolved
    # only by the existing owner-scoped foreground service and the server's
    # explicit allowed-model set, never from a direct request or a button.
    fallback_options = {}
    if settings.enable_model_fallback:
        prepared = resolve_admitted_foreground_policy(
            admission, allowed_models=settings.allowed_fallback_models,
        )
        if prepared.enabled:
            candidates, descriptors = build_admitted_foreground_routes(
                admission, prepared,
                endpoint_url=session.endpoint_url, model=session.model,
                headers=getattr(session, "headers", None),
                selected_endpoint_id=getattr(session, "endpoint_id", None),
            )
            if (
                not candidates or len(candidates) != len(descriptors)
                or candidates[0][:2] != (session.endpoint_url, session.model)
                or any(
                    not isinstance(item, (tuple, list)) or len(item) != 3
                    or type(item[0]) is not str or not item[0].strip()
                    or type(item[1]) is not str
                    or item[1] not in settings.allowed_fallback_models
                    or not isinstance(item[2], dict)
                    for item in candidates[1:]
                )
            ):
                raise AdmissionDenied(409, "Invalid admitted fallback route selection")
            if len(candidates) > 1:
                fallback_options = {
                    "fallbacks": candidates[1:],
                    "route_descriptors": descriptors,
                    "fallback_statuses": prepared.eligible_statuses,
                    "fallback_on_empty": prepared.fallback_on_empty,
                    "defer_context_shaping": True,
                }

    resolution = None
    if continuation:
        # Context preprocessing is asynchronous. Workspace selection must
        # still match the loaded session immediately before consuming the
        # one-use approval. A changed path leaves the card available.
        if _admitted_session_workspace(session, settings.workspace) != workspace:
            raise AdmissionDenied(409, "Workspace changed during approval preparation")
        if filesystem_bound:
            require_sealed_filesystem_approval(
                pending, owner=owner, session_id=session_id, workspace=workspace,
            )
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
        workspace=workspace,
        turn_contract=contract,
        max_rounds=settings.max_rounds,
        max_tokens=settings.max_tokens,
        context_length=context.context_length,
        uploaded_files=[],
        active_document=active_document,
        approval_store=store,
        **fallback_options,
        **({
            "exact_approval": resolution.exact,
            "external_untrusted_context_seen": resolution.tainted,
            "relevant_tools": (
                {pending.tool_name} if filesystem_bound
                else set(pending.selected_tools) & settings.offered_tools
            ),
        } if resolution is not None else {"external_untrusted_context_seen": retired_taint}),
        native_terminal_events=native_terminal_events,
        native_tool_events=native_tool_events,
        native_route_events=native_route_events,
    )) as stream:
        async for chunk in stream:
            yield chunk
