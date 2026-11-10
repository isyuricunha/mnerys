"""Internal Gateway-facing authority and run controls for an admitted session.

This facade is not a new HTTP API or a credential verifier. Every operation
requires an AdmittedSession produced by the Gateway session policy. It keeps
Web adapters from calling the unrestricted legacy run manager directly.
"""

from contextlib import aclosing
from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any, AsyncGenerator, Callable, Iterable, Optional
from uuid import uuid4

from src.agent_runtime.authority import RequestAuthority
from src.action_intents import ToolIntent
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.gateway_foreground_routes import (
    AdmittedForegroundPolicy,
    build_admitted_foreground_routes,
    resolve_admitted_foreground_policy,
)
from src.gateway_foreground_context import admitted_candidate_request_factory
from src.gateway_prompt_preparation import (
    append_admitted_internal_context,
    select_admitted_prompt_messages,
)
from src.gateway_approval_decisions import ToolApprovalDecision, consume_admitted_tool_approval, inspect_admitted_tool_approval
from src.gateway_turn_authority import authority_for_admitted_turn
from src.tool_policy import ToolPolicy
from src.tool_approvals import (
    ExactToolApproval, ToolApprovalStore, tool_approval_store,
    bind_gateway_approval_origin,
)
from src.gateway_web_approval_cards import read_admitted_pending_review
from src.turn_service import TurnService, TurnStream, TypedTurnStream, turn_service
from src.gateway_stream_delivery import TurnDelivery, open_admitted_stream
from src.gateway_post_response import dispatch_admitted_post_response
from src.gateway_routed_contract import (
    AdmittedRoutedContract,
    resolve_admitted_routed_contract,
)
from src.gateway_tool_inventory import (
    AdmittedToolInventory,
    prepare_admitted_tool_inventory,
)
from src.gateway_post_session_routing import (
    AdmittedPostSessionRoute,
    AdmittedDocumentEditRoute,
    reconcile_admitted_post_session_route,
    promote_admitted_document_edit_route,
)
from src.agent_core_events import CoreNativeEvent
from src.gateway_agent_execution import stream_admitted_agent_turn
from src.gateway_turn_events import (
    TurnOutcome,
    TypedTurnDelivery,
    translate_legacy_turn_events,
)
from src.gateway_assistant_persistence import (
    save_admitted_assistant_response,
    save_admitted_interrupted_assistant_response,
)
from src.gateway_turn_composer import (
    TextTurnRequest, ToolApprovalTurnRequest,
    TrustedTextTurnSettings,
    stream_composed_text_turn,
    _ACTIVE_EMAIL_REVIEW_DISABLED_TOOLS,
)
from src.gateway_text_persistence import (
    TrustedTextPersistence, persist_completed_text_events,
    require_trusted_text_persistence,
    require_persisted_native_review_origin,
)
from src.gateway_web_compat_events import serialize_plain_text_web_sse
from src.gateway_context_ports import ChatContextPorts
from src.gateway_chat_context import assemble_chat_context
from src.tool_execution import AgentExecutionBridge
from src.gateway_turn_runtime_state import (
    AdmittedRuntimeUIState,
    project_admitted_runtime_ui_state,
)
from src.gateway_initial_web_intent import (
    InitialWebIntent,
    classify_initial_web_intent,
)
from src.gateway_initial_agent_routing import (
    InitialAgentRoute,
    select_initial_agent_route,
)
from src.gateway_web_intent import (
    AdmittedWebIntent,
    reconcile_admitted_web_intent,
)
from src.gateway_turn_capabilities import (
    AdmittedTurnCapabilities,
    classify_admitted_turn_capabilities,
)
from src.gateway_tool_intent import (
    AdmittedToolIntent,
    select_admitted_tool_intent,
)
from src.gateway_contract_scoping import (
    AdmittedScopedContract,
    scope_admitted_routed_contract,
)
from src.gateway_turn_contract_selection import (
    AdmittedContractScope,
    select_admitted_compact_runtime,
    select_admitted_contract_scope,
)
from src.gateway_execution_policy import (
    FinalTurnToolPolicy,
    finalize_admitted_turn_tool_policy,
    execution_limits_for_admission,
)
from src.tool_policy import build_effective_tool_policy


@dataclass(frozen=True)
class WebTurnComposition:
    """Everything a trusted Web adapter supplies for one production turn.

    Untrusted request data arrives pre-parsed and normalized by the adapter.
    The composition re-derives every authority from the same admitted
    Gateway services, in the inherited Web turn's exact order, so the HTTP
    layer keeps only parsing, serialization and session/endpoint recovery.
    """

    # Issued proof and loaded session
    admission: AdmittedSession
    session: Any
    session_id: str
    # The trusted adapter may inject its own sealed approval store; the
    # process-wide singleton remains the default.
    approval_store: ToolApprovalStore | None = None
    # Pre-normalized request inputs (the adapter owns parsing/validation)
    message: str = ""
    chat_mode: str = ""
    att_ids: tuple[str, ...] = ()
    search_enabled: bool = False
    use_web: Any = None
    use_rag: Any = None
    no_memory: bool = False
    incognito: bool = False
    compare_mode: bool = False
    search_context: str | None = None
    preset_id: str | None = None
    thinking_mode: str | None = None
    reasoning_effort: str | None = None
    temperature_override: float | None = None
    plan_mode: bool = False
    approved_plan: str = ""
    allow_bash: Any = None
    allow_web_search: Any = None
    tool_approval_id: str | None = None
    tool_approval_decision: str | None = None
    workspace: str = ""
    active_doc_id: str = ""
    active_doc_state: str = ""
    active_email: dict | None = None
    selected_notes_context: str | None = None
    client_runtime_context: dict | None = None
    internal_tool_request: bool = False
    experiment_header: str | None = None
    # Pre-admission pure routing results (pure helpers, no admission)
    initial_tool_intent: ToolIntent | None = None
    initial_workspace_agent_intent: bool = False
    initial_auto_escalated: bool = False
    # Server-owned adapter dependencies
    chat_handler: Any = None
    chat_processor: Any = None
    ports: ChatContextPorts | None = None
    privileges: dict | None = None
    global_disabled_tools: tuple[str, ...] = ()
    browser_tools: frozenset[str] = frozenset()
    allowed_models: Iterable[str] | None = None
    # Server-owned, lazy Web evidence lookups
    initial_local_browser_render_lookup: Callable[[], bool] = lambda: False
    recent_private_browser_success_lookup: Callable[[], bool] = lambda: False
    contextual_web_followup_lookup: Callable[[], bool] = lambda: False
    contextual_browser_followup_lookup: Callable[[], bool] = lambda: False
    workspace_from_message_lookup: Callable[[], tuple] = lambda: ("", None)
    web_link_followup_lookup: Callable[[], bool] = lambda: False
    recent_web_event_lookup: Callable[[], bool] = lambda: False
    message_denies_web_lookup: Callable[[], bool] = lambda: False
    successful_session_tool_names: Callable[[], frozenset] = lambda: frozenset()
    most_recent_successful_web_tool: Callable[[], object] = lambda: None
    structured_document_intent_lookup: Callable[[], bool] = lambda: False
    on_user_message_persisted: Callable[[], None] | None = None
    # Adapter-resolved session state
    research_requested: bool = False
    research_pending: bool = False
    image_generation_session: bool = False
    tool_schema_full_route: bool = False
    clean_route_requested: bool = False
    compact_context_resolution: Any = None
    selected_endpoint_id: str = ""


@dataclass(frozen=True)
class PreparedWebTurn:
    """Immutable result of composing one admitted Web turn.

    Authority, contract and policy objects come only from the admitted
    Gateway services; they are never deserialized from a client request.
    Denied approval continuations return without composing a turn, so the
    execution-facing fields are Optional only on that control-plane path.
    """

    context_owner: str
    context: Any
    message: str
    workspace: str | None
    chat_mode: str
    auto_escalated: bool
    foreground_policy: AdmittedForegroundPolicy | None
    foreground_fallback_models: frozenset[str]
    final_policy: FinalTurnToolPolicy | None
    tool_policy: ToolPolicy | None
    disabled_tools: frozenset[str]
    request_authority: RequestAuthority | None
    turn_contract: Any
    active_document: Any
    active_email: dict | None
    client_runtime_context: dict | None
    effective_do_research: bool
    research_blocked_by_policy: bool
    image_generation_session: bool
    # Web intent/evidence results the execution branch consumes
    search_enabled: bool = False
    explicit_web_intent: bool = False
    explicit_browser_intent: bool = False
    tool_intent: ToolIntent | None = None
    workspace_agent_intent: bool = False
    image_editing_requested: bool = False
    foreground_candidates: list | None = None
    foreground_route_descriptors: list | None = None
    chat_request_factory: Any = None
    compact_preview: Any = None
    # Approval continuation state (already admitted, not yet consumed)
    pending_tool_approval: Any = None
    exact_tool_approval: Any = None
    approval_decision: str | None = None
    approval_denied: bool = False
    external_untrusted_context_seen: bool = False


def _require_persisted_notes_web_profile(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    request: TextTurnRequest | ToolApprovalTurnRequest,
    settings: TrustedTextTurnSettings,
    persistence: TrustedTextPersistence,
    approval_store: ToolApprovalStore | None,
) -> tuple[str, TrustedTextPersistence, ToolApprovalStore]:
    """Preflight a trusted notes adapter before replacing an in-flight run.

    This checks only the sealed review and original receipt; the composer
    still rechecks account policy, request authority and one-use consumption.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if owner != context_owner:
        raise AdmissionDenied(403, "Persisted notes preview owner mismatch")
    if (
        not isinstance(request, (TextTurnRequest, ToolApprovalTurnRequest))
        or (isinstance(request, TextTurnRequest) and
            (type(request.message) is not str or not request.message.strip()
             or request.attachment_ids != ()))
        or not isinstance(settings, TrustedTextTurnSettings)
        or settings.offered_tools != frozenset({"manage_notes"})
        or settings.interaction_mode != "agent"
        or settings.persist_verified_native_tools is not True
        or settings.workspace is not None
        or settings.active_document_id is not None
        or settings.persist_verified_text_attachments
        or settings.enable_model_fallback
        or settings.allowed_fallback_models
    ):
        raise AdmissionDenied(403, "Unsupported persisted notes Web preview")
    writer = require_trusted_text_persistence(persistence)
    store = tool_approval_store if approval_store is None else approval_store
    if not isinstance(store, ToolApprovalStore):
        raise AdmissionDenied(403, "Invalid persisted review store")
    if isinstance(request, ToolApprovalTurnRequest):
        if type(request.decision) is not str or request.decision not in {
            "approve", "approve_task", "deny",
        }:
            raise AdmissionDenied(403, "Invalid persisted review decision")
        if admission.principal.delegated:
            raise AdmissionDenied(403, "A delegated token cannot approve an action")
        read_admitted_pending_review(
            admission, session, store, request.approval_id,
            context_owner=context_owner,
        )
        pending = store.peek(request.approval_id)
        if pending is None:
            raise AdmissionDenied(409, "Pending review no longer exists")
        require_persisted_native_review_origin(session, pending)
    return owner, writer, store



class GatewayTurns:
    def __init__(self, turns: TurnService):
        self._turns = turns

    def resolve_foreground_policy(
        self,
        admission: AdmittedSession,
        *,
        allowed_models: Iterable[str] | None,
    ) -> AdmittedForegroundPolicy:
        """Use only the admitted owner when reading fallback preferences."""
        return resolve_admitted_foreground_policy(
            admission, allowed_models=allowed_models,
        )

    def build_foreground_routes(
        self,
        admission: AdmittedSession,
        policy: AdmittedForegroundPolicy,
        *,
        endpoint_url: str,
        model: str,
        headers: dict[str, Any] | None,
        selected_endpoint_id: str | None = None,
    ) -> tuple[list, list]:
        """Prepare owner-scoped fallback candidates and safe route metadata."""
        return build_admitted_foreground_routes(
            admission, policy, endpoint_url=endpoint_url, model=model,
            headers=headers, selected_endpoint_id=selected_endpoint_id,
        )

    def build_candidate_context_factory(
        self,
        admission: AdmittedSession,
        policy: AdmittedForegroundPolicy,
        messages: list,
        fallback_context_length: int = 0,
        *,
        session: Any,
    ):
        """Prepare route-neutral context per candidate without writing it."""
        return admitted_candidate_request_factory(
            admission, policy, messages, fallback_context_length,
            session=session,
        )

    def append_internal_context(
        self,
        admission: AdmittedSession,
        policy: AdmittedForegroundPolicy,
        context: Any,
        request_text: str,
        *,
        internal_tool_request: bool,
    ) -> None:
        """Guard internal tool-loopback content without turning it into consent."""
        append_admitted_internal_context(
            admission, policy, context, request_text,
            internal_tool_request=internal_tool_request,
        )

    def select_prompt_messages(
        self,
        admission: AdmittedSession,
        policy: AdmittedForegroundPolicy,
        context: Any,
        request_text: str,
        *,
        streaming: bool,
        approval_continuation: bool = False,
    ) -> list:
        """Select final prompt history only for the issued, admitted turn."""
        return select_admitted_prompt_messages(
            admission, policy, context, request_text,
            streaming=streaming,
            approval_continuation=approval_continuation,
        )

    async def compose_web_turn(self, composition: WebTurnComposition) -> PreparedWebTurn:
        """Compose the full production Web turn from admitted services.

        One transport-neutral operation runs the inherited Web chain in its
        exact order: early intents, post-session routing, context ports,
        compact runtime, document promotion, contract scope, capability
        classification, delegated limits, web intent reconciliation,
        incognito/email tool strips, final policy veto, runtime UI
        projection, request authority, inventory/selection and the routed
        contract. It performs no model execution, message saves or HTTP I/O;
        the adapter serializes and the execution boundary enters the Agent
        Loop.
        """
        admission = composition.admission
        session = composition.session
        session_id = composition.session_id
        require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if not isinstance(self._turns, TurnService):
            raise AdmissionDenied(403, "Invalid server agent runtime")
        if not callable(getattr(composition.ports, "extract_preset", None)):
            raise AdmissionDenied(403, "Trusted chat context ports are required")
        if (
            type(composition.message) is not str
            # A tool-approval continuation is a control-plane decision: the
            # client intentionally submits an empty composer text and the
            # sealed pending approval restores the originating query below.
            or (
                not composition.tool_approval_id
                and not composition.message.strip()
            )
            or type(composition.att_ids) is not tuple
            or any(type(item) is not str or not item.strip() for item in composition.att_ids)
            or type(composition.chat_mode) is not str
        ):
            raise AdmissionDenied(403, "Invalid composed Web turn inputs")

        # Early pure intent/routing hints. The inherited route classifies
        # these before admission; they are routing hints, never grants.
        _search_enabled = bool(composition.search_enabled)
        initial = self.preview_initial_web_intent(
            message=composition.message,
            search_enabled=_search_enabled,
            workspace_bound=bool(composition.workspace),
            local_browser_render_lookup=composition.initial_local_browser_render_lookup,
        )
        _explicit_web_intent = initial.explicit_web_intent
        _explicit_personal_store_intent = initial.personal_store_intent
        _exact_url_fetch_intent = initial.exact_url_fetch_intent
        _explicit_browser_intent = initial.explicit_browser_intent
        _external_discovery_intent = initial.external_discovery_intent
        _explicit_private_browser_intent = initial.explicit_private_browser_intent
        _local_browser_render_intent = initial.local_browser_render_intent
        _allow_browser_for_web_turn = initial.allow_browser_for_web_turn

        chat_mode = composition.chat_mode
        plan_mode = bool(composition.plan_mode)
        if plan_mode:
            chat_mode = "agent"
        initial_route = self.preview_initial_agent_route(
            message=composition.message,
            chat_mode=chat_mode,
            allow_bash=composition.allow_bash,
            search_enabled=_search_enabled,
            explicit_web_intent=_explicit_web_intent,
            explicit_private_browser_intent=_explicit_private_browser_intent,
            native_workspace_inputs=bool(composition.client_runtime_context),
        )
        chat_mode = initial_route.chat_mode
        _tool_intent = initial_route.tool_intent
        _workspace_agent_intent = initial_route.workspace_agent_intent
        auto_escalated = initial_route.auto_escalated
        allow_bash = initial_route.allow_bash

        # One admitted approval decision is consumed by the control plane,
        # never by a button label. Denial closes without context or model I/O.
        pending_tool_approval = None
        exact_tool_approval = None
        approval_decision = None
        tool_approval_continuation = False
        external_untrusted_context_seen = False
        if composition.tool_approval_id:
            store = composition.approval_store or tool_approval_store
            _, requested_decision = inspect_admitted_tool_approval(
                admission, composition.tool_approval_id,
                composition.tool_approval_decision, plan_mode=plan_mode,
                internal_tool_request=composition.internal_tool_request, store=store,
            )
            if requested_decision != "deny" and not execution_limits_for_admission(
                admission, composition.privileges, browser_tools=composition.browser_tools,
            ).allow_agent:
                raise AdmissionDenied(403, "Agent turns are not allowed for this account")
            resolution = self.decide_tool_approval(
                admission,
                composition.tool_approval_id,
                composition.tool_approval_decision,
                plan_mode=plan_mode,
                internal_tool_request=composition.internal_tool_request,
                store=store,
            )
            pending_tool_approval = resolution.pending
            exact_tool_approval = resolution.exact
            approval_decision = resolution.decision
            external_untrusted_context_seen = external_untrusted_context_seen or resolution.tainted
            tool_approval_continuation = True
            if resolution.decision == "deny":
                return PreparedWebTurn(
                    context_owner=owner, context=None, message=composition.message,
                    workspace=composition.workspace, chat_mode=chat_mode,
                    auto_escalated=auto_escalated,
                    foreground_policy=None, foreground_fallback_models=frozenset(),
                    final_policy=None, tool_policy=None,
                    disabled_tools=frozenset(), request_authority=None,
                    turn_contract=None, active_document=None,
                    active_email=composition.active_email,
                    client_runtime_context=composition.client_runtime_context,
                    effective_do_research=False,
                    research_blocked_by_policy=False,
                    image_generation_session=composition.image_generation_session,
                    pending_tool_approval=pending_tool_approval,
                    exact_tool_approval=exact_tool_approval,
                    approval_decision=approval_decision,
                    approval_denied=True,
                    external_untrusted_context_seen=external_untrusted_context_seen,
                )
            # An approval is a control-plane continuation: the sealed record,
            # not mutable form state, restores the originating context.
            message = pending_tool_approval.continuation_query
            workspace = pending_tool_approval.workspace or ""
            active_doc_id = pending_tool_approval.document_id or ""
            # The approved operation resumes the agent runtime regardless of
            # the current composer toggle.  The sealed authority, current
            # policy and exact claim still determine what may actually run.
            chat_mode = "agent"
        else:
            message = composition.message
            workspace = composition.workspace
            active_doc_id = composition.active_doc_id
            # A normal user message supersedes a waiting card; preserve its
            # security taint for this turn.
            retired_taint = (composition.approval_store or tool_approval_store).retire_for_session(
                owner=owner, session_id=session_id,
            )
            external_untrusted_context_seen = external_untrusted_context_seen or retired_taint

        # Post-session routing reconciles the persisted compact-route hint
        # and contextual browser/web escalation after ownership is verified.
        post_session = self.reconcile_post_session_route(
            admission, session,
            context_owner=owner,
            message=message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            tool_intent=_tool_intent,
            workspace_agent_intent=_workspace_agent_intent,
            workspace=workspace,
            allow_bash=allow_bash,
            explicit_browser_intent=_explicit_browser_intent,
            clean_route_requested=composition.clean_route_requested,
            recent_private_browser_success_lookup=(
                composition.recent_private_browser_success_lookup
            ),
            contextual_web_followup_lookup=composition.contextual_web_followup_lookup,
            contextual_browser_followup_lookup=(
                composition.contextual_browser_followup_lookup
            ),
            workspace_from_message_lookup=composition.workspace_from_message_lookup,
        )
        chat_mode = post_session.chat_mode
        auto_escalated = post_session.auto_escalated
        _tool_intent = post_session.tool_intent
        _workspace_agent_intent = post_session.workspace_agent_intent
        workspace = post_session.workspace
        allow_bash = post_session.allow_bash
        _explicit_browser_intent = post_session.explicit_browser_intent
        _clean_v3_private_browser_warm = post_session.private_browser_warm
        _contextual_browser_turn_followup = post_session.contextual_browser_followup

        # Privilege gates: allowed models and message quotas are adapter
        # checks; the account agent permission is enforced with the policy.
        do_research = bool(composition.research_requested) and not tool_approval_continuation
        if not do_research and not tool_approval_continuation and composition.research_pending:
            do_research = True

        pre_context_tool_policy = build_effective_tool_policy(
            last_user_message=message,
        )
        allow_tool_preprocessing = not pre_context_tool_policy.block_all_tool_calls
        foreground_policy = self.resolve_foreground_policy(
            admission,
            allowed_models=composition.allowed_models,
        )
        _foreground_fallback_models = (
            frozenset(
                candidate[1]
                for candidate in foreground_policy.policy.fallback_candidates
            )
            if foreground_policy.enabled else frozenset()
        )

        compact_preview = self.select_compact_runtime(
            admission, session,
            context_owner=owner,
            clean_route_requested=composition.clean_route_requested,
            full_schema_route=composition.tool_schema_full_route,
            exact_tool_approval=exact_tool_approval,
            runtime_surface=str(
                (composition.client_runtime_context or {}).get("surface") or ""
            ),
            agent_mode=(chat_mode == "agent"),
            agent_permitted=bool((composition.privileges or {}).get("can_use_agent", True)),
            image_generation=composition.image_generation_session,
        )
        context = await assemble_chat_context(
            session, owner, composition.chat_handler, composition.chat_processor,
            message=message,
            session_id=session_id,
            selected_notes_context=composition.selected_notes_context,
            preset_id=composition.preset_id,
            att_ids=list(composition.att_ids),
            use_web=composition.use_web,
            use_rag=composition.use_rag,
            use_research="true" if composition.research_requested else None,
            incognito=composition.incognito,
            no_memory=composition.no_memory,
            search_context=composition.search_context,
            compare_mode=composition.compare_mode,
            use_enhanced_message=True,
            agent_mode=(chat_mode == "agent"),
            allow_tool_preprocessing=allow_tool_preprocessing,
            defer_context_shaping=foreground_policy.enabled,
            continuation_context_message=(
                pending_tool_approval.continuation_query
                if exact_tool_approval and pending_tool_approval
                and pending_tool_approval.continuation_query
                else None
            ),
            persist_user_message=not tool_approval_continuation
            and not composition.internal_tool_request,
            context_resolution=composition.compact_context_resolution,
            interaction_mode=chat_mode or None,
            auto_escalated=auto_escalated,
            ports=composition.ports,
            on_user_message_persisted=composition.on_user_message_persisted,
        )
        self.append_internal_context(
            admission, foreground_policy, context, message,
            internal_tool_request=composition.internal_tool_request,
        )
        _foreground_candidates, _foreground_route_descriptors = (
            self.build_foreground_routes(
                admission, foreground_policy,
                endpoint_url=session.endpoint_url,
                model=session.model,
                headers=getattr(session, "headers", None),
                selected_endpoint_id=(
                    composition.selected_endpoint_id
                    or getattr(session, "endpoint_id", None)
                ),
            )
            if foreground_policy.enabled else (None, None)
        )
        _chat_request_factory = None
        if foreground_policy.enabled:
            _chat_request_factory, _ = self.build_candidate_context_factory(
                admission, foreground_policy,
                context.messages, getattr(context, "context_length", 0),
                session=session,
            )

        # The adapter resolves the active document through its existing
        # owner/session-checked lookups; promotion still requires admission.
        document_edit_route = self.promote_document_edit_route(
            admission, session,
            context_owner=owner,
            message=message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            active_document=bool(active_doc_id),
        )
        chat_mode = document_edit_route.chat_mode
        auto_escalated = document_edit_route.auto_escalated

        contract_scope = self.select_contract_scope(
            admission, session,
            context_owner=owner,
            client_runtime_context=composition.client_runtime_context,
            workspace_agent_intent=_workspace_agent_intent,
            workspace_bound=bool(workspace),
            clean_route_requested=composition.clean_route_requested,
            full_schema_route=composition.tool_schema_full_route,
            exact_tool_approval=exact_tool_approval,
        )
        _native_workspace_contract = contract_scope.native_workspace_contract
        _use_turn_contract = contract_scope.enabled
        _turn_history = getattr(session, "history", []) or []
        initial_capabilities = self.classify_turn_capabilities(
            admission, session,
            context_owner=owner,
            message=message,
            use_turn_contract=_use_turn_contract,
            active_document=bool(active_doc_id),
            workspace_bound=bool(workspace),
            image_attachment=False,
            explicit_browser_intent=_explicit_browser_intent,
            external_discovery_intent=_external_discovery_intent,
            private_browser_warm=_clean_v3_private_browser_warm,
            contextual_browser_followup_lookup=(
                composition.contextual_browser_followup_lookup
            ),
        )
        _corrected_browser_target = initial_capabilities.corrected_browser_target
        _turn_capabilities = initial_capabilities.capabilities
        _active_turn_capabilities = initial_capabilities.active_capabilities
        if initial_capabilities.suppress_explicit_web_intent:
            _explicit_web_intent = False

        disabled_tools = set(execution_limits_for_admission(
            admission, None,
            browser_tools=composition.browser_tools,
        ).disabled_tools)
        if allow_bash is not None and str(allow_bash).lower() != "true":
            disabled_tools.add("bash")
        web_intent = self.reconcile_web_intent(
            admission, session,
            context_owner=owner,
            message=message,
            explicit_web_intent=_explicit_web_intent,
            personal_store_intent=_explicit_personal_store_intent,
            tool_intent_is_web=bool(
                _tool_intent and _tool_intent.category == "web"
            ),
            capabilities=_turn_capabilities,
            clean_route_requested=composition.clean_route_requested,
            allow_web_search=composition.allow_web_search,
            search_enabled=_search_enabled,
            explicit_browser_intent=_explicit_browser_intent,
            local_browser_render_intent=_local_browser_render_intent,
            exact_url_fetch_intent=_exact_url_fetch_intent,
            use_turn_contract=_use_turn_contract,
            full_schema_route=composition.tool_schema_full_route,
            workspace_bound=bool(workspace),
            base_disabled_tools=disabled_tools,
            web_link_followup_lookup=composition.web_link_followup_lookup,
            contextual_web_followup_lookup=composition.contextual_web_followup_lookup,
            recent_web_event_lookup=composition.recent_web_event_lookup,
            message_denies_lookup=composition.message_denies_web_lookup,
        )
        _explicit_web_intent = web_intent.explicit_web_intent
        _contextual_web_turn_followup = web_intent.contextual_web_turn_followup
        _search_enabled = web_intent.search_enabled
        disabled_tools = set(web_intent.disabled_tools)

        if composition.incognito:
            disabled_tools.update({
                "manage_memory", "search_chats", "manage_skills",
                "create_session", "list_sessions", "manage_session",
                "send_to_session", "chat_with_model",
            })
        if composition.active_email and composition.active_email.get("uid"):
            disabled_tools.update(_ACTIVE_EMAIL_REVIEW_DISABLED_TOOLS)

        final_policy = self.finalize_tool_policy(
            admission, session,
            context_owner=owner,
            base_disabled_tools=disabled_tools,
            privileges=composition.privileges,
            global_disabled_tools=composition.global_disabled_tools,
            browser_tools=composition.browser_tools,
            last_user_message=message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            workspace_agent_intent=_workspace_agent_intent,
            use_turn_contract=_use_turn_contract,
            allow_browser_for_web_turn=_allow_browser_for_web_turn,
            compare_mode=composition.compare_mode,
            plan_mode=plan_mode,
        )
        if not final_policy.allow_agent:
            chat_mode = "chat"
        chat_mode = final_policy.chat_mode
        tool_policy = final_policy.policy
        disabled_tools = tool_policy.all_disabled_names()

        runtime_ui = self.project_runtime_ui_state(
            admission, session,
            context_owner=owner,
            client_runtime_context=composition.client_runtime_context,
            policy=tool_policy,
            use_rag=composition.use_rag,
            use_research=None,
            incognito=bool(composition.incognito),
        )
        client_runtime_context = runtime_ui.context

        request_authority = self.build_authority(
            admission, message,
            workspace=workspace,
            history=_turn_history,
            policy=tool_policy,
            client_runtime_context=client_runtime_context,
            active_document=bool(active_doc_id),
            image_attachment=False,
            capabilities=(
                {"search_browser"}
                if (_explicit_browser_intent or _external_discovery_intent)
                else ()
            ),
            internal_tool_request=composition.internal_tool_request,
            exact_approval=exact_tool_approval,
        )
        turn_contract = None
        if _use_turn_contract and chat_mode == "agent" and not composition.image_generation_session:
            tool_inventory = self.prepare_tool_inventory(
                admission, session,
                context_owner=owner,
                base_policy=tool_policy,
                base_disabled_tools=disabled_tools,
                message=message,
                plan_mode=plan_mode,
                native_workspace_contract=_native_workspace_contract,
            )
            disabled_tools = set(tool_inventory.disabled_tools)
            _contract_policy = tool_inventory.policy
            selected_intent = self.select_tool_intent(
                admission, session,
                context_owner=owner,
                inventory=tool_inventory,
                message=message,
                capabilities=_turn_capabilities,
                active_capabilities=_active_turn_capabilities,
                warm_tools=composition.successful_session_tool_names(),
                recent_web_tool_lookup=composition.most_recent_successful_web_tool,
                active_document=bool(active_doc_id),
                contextual_browser_followup=_contextual_browser_turn_followup,
                contextual_web_followup=_contextual_web_turn_followup,
                active_email_review=bool(
                    composition.active_email
                    and composition.active_email.get("uid")
                ),
                corrected_browser_target=bool(_corrected_browser_target),
            )
            _warm_tools = selected_intent.warm_tools
            _selected_tools = selected_intent.selected_tools
            _required_tools = selected_intent.required_tools
            _turn_capabilities = selected_intent.capabilities
            _active_turn_capabilities = selected_intent.active_capabilities
            _contract_policy = selected_intent.policy
            resolved = self.resolve_routed_contract(
                admission, session,
                context_owner=owner,
                request_authority=request_authority,
                capabilities=_turn_capabilities,
                schemas=tool_inventory.schemas,
                policy=_contract_policy,
                required_tools=_required_tools,
                required_capabilities=_active_turn_capabilities,
                selected_tools=_selected_tools,
                always_available_tools=(("read_document",) if active_doc_id else ()),
                warm_tools=_warm_tools,
                message=message,
                history=_turn_history,
            )
            scoped = self.scope_routed_contract(
                admission, session,
                context_owner=owner,
                routed=resolved,
                schemas=tool_inventory.schemas,
                contract_policy=_contract_policy,
                disabled_tools=disabled_tools,
                active_capabilities=_active_turn_capabilities,
                selected_tools=_selected_tools,
                warm_tools=_warm_tools,
                compact_preview=compact_preview,
                native_workspace_contract=_native_workspace_contract,
                structured_document_intent=bool(
                    compact_preview
                    and _native_workspace_contract
                    and composition.structured_document_intent_lookup()
                ),
                local_browser_render_intent=_local_browser_render_intent,
                experiment_header=(
                    composition.experiment_header if compact_preview else None
                ),
                message=message,
                history=_turn_history,
                browser_requested=_explicit_browser_intent,
            )
            turn_contract = scoped.contract
            request_authority = scoped.authority
            disabled_tools = set(scoped.disabled_tools)
            tool_policy = scoped.policy

        research_blocked_by_policy = bool(
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        effective_do_research = bool(
            do_research and not research_blocked_by_policy
        )

        return PreparedWebTurn(
            context_owner=owner,
            context=context,
            message=message,
            workspace=workspace,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            foreground_policy=foreground_policy,
            foreground_fallback_models=_foreground_fallback_models,
            final_policy=final_policy,
            tool_policy=tool_policy,
            disabled_tools=frozenset(disabled_tools),
            request_authority=request_authority,
            turn_contract=turn_contract,
            active_document=None,
            active_email=composition.active_email,
            client_runtime_context=client_runtime_context,
            effective_do_research=effective_do_research,
            research_blocked_by_policy=research_blocked_by_policy,
            image_generation_session=composition.image_generation_session,
            pending_tool_approval=pending_tool_approval,
            exact_tool_approval=exact_tool_approval,
            approval_decision=approval_decision,
            approval_denied=False,
            external_untrusted_context_seen=external_untrusted_context_seen,
            search_enabled=_search_enabled,
            explicit_web_intent=_explicit_web_intent,
            explicit_browser_intent=_explicit_browser_intent,
            tool_intent=_tool_intent,
            workspace_agent_intent=_workspace_agent_intent,
            image_editing_requested=(
                initial_capabilities.image_editing_requested
            ),
            foreground_candidates=_foreground_candidates,
            foreground_route_descriptors=_foreground_route_descriptors,
            chat_request_factory=_chat_request_factory,
            compact_preview=compact_preview,
        )

    def build_authority(
        self,
        admission: AdmittedSession,
        request_text: str,
        *,
        workspace: str | None,
        history: Any,
        policy: ToolPolicy,
        client_runtime_context: Any = None,
        active_document: bool = False,
        image_attachment: bool = False,
        capabilities: Iterable[str] = (),
        internal_tool_request: bool,
        exact_approval: ExactToolApproval | None = None,
    ) -> RequestAuthority:
        """Build server-owned authority before this admitted session executes."""
        return authority_for_admitted_turn(
            admission,
            request_text,
            workspace=workspace,
            history=history,
            policy=policy,
            client_runtime_context=client_runtime_context,
            active_document=active_document,
            image_attachment=image_attachment,
            capabilities=capabilities,
            internal_tool_request=internal_tool_request,
            exact_approval=exact_approval,
        )

    def decide_tool_approval(
        self,
        admission: AdmittedSession,
        approval_id: Any,
        decision: Any,
        *,
        plan_mode: bool,
        internal_tool_request: bool,
        store: ToolApprovalStore,
        records: Any = None,
        session: Any = None,
    ) -> ToolApprovalDecision:
        """Validate and consume one existing approval under trusted admission.

        With ``records`` (a SqlTurnRecords) and the owning ``session``, the
        sealed decision is durably recorded after consumption.
        """
        return consume_admitted_tool_approval(
            admission,
            approval_id,
            decision,
            plan_mode=plan_mode,
            internal_tool_request=internal_tool_request,
            store=store,
            records=records,
            session=session,
        )

    def save_interrupted_assistant_response(
        self,
        admission: AdmittedSession,
        session: Any,
        session_manager: Any,
        claimed_session_id: str,
        partial_content: str,
        stopped_metadata: dict,
        *,
        context_owner: str | None,
        writer: Callable[..., Any],
        incognito: bool = False,
    ) -> Any:
        """Validate a stopped partial before invoking the original writer."""
        return save_admitted_interrupted_assistant_response(
            admission, session, session_manager, claimed_session_id,
            partial_content, stopped_metadata,
            context_owner=context_owner, writer=writer, incognito=incognito,
        )

    def save_assistant_response(
        self,
        admission: AdmittedSession,
        session: Any,
        session_manager: Any,
        claimed_session_id: str,
        full_response: str,
        last_metrics: dict | None,
        *,
        context_owner: str | None,
        saver: Callable[..., Any],
        character_name: str | None = None,
        web_sources: list | None = None,
        rag_sources: list | None = None,
        research_sources: list | None = None,
        used_memories: list | None = None,
        do_research: bool = False,
        tool_events: list | None = None,
        incognito: bool = False,
    ) -> Any:
        """Persist through the inherited writer after owner-bound admission."""
        return save_admitted_assistant_response(
            admission, session, session_manager, claimed_session_id,
            full_response, last_metrics,
            context_owner=context_owner,
            saver=saver,
            character_name=character_name,
            web_sources=web_sources,
            rag_sources=rag_sources,
            research_sources=research_sources,
            used_memories=used_memories,
            do_research=do_research,
            tool_events=tool_events,
            incognito=incognito,
        )

    def open_composed_text_events(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        approval_store: ToolApprovalStore | None = None,
        persistence: TrustedTextPersistence | None = None,
        client_rendering: bool = False,
        records: Any = None,
        turn_id: str | None = None,
    ) -> TypedTurnDelivery:
        """Offer typed process-local events from the safe direct composer.

        The returned iterator performs admitted validation on first consume;
        this is neither a remote API nor a durable replayable event stream.
        With ``client_rendering``, the trusted client adapter receives the
        browser-facing frame families it owns instead of filtered metadata.
        With ``records`` (a SqlTurnRecords) and an optional ``turn_id``, a
        durable owner-scoped turn record is opened and every typed event is
        persisted with sanitized payloads until an exactly-once terminal.
        """
        if type(client_rendering) is not bool:
            raise AdmissionDenied(403, "Invalid trusted client rendering flag")
        durable_records = None
        durable_turn_id = None
        if records is not None:
            from src.gateway_turn_records import SqlTurnRecords, TurnRecordError
            if not isinstance(records, SqlTurnRecords):
                raise AdmissionDenied(403, "Invalid durable turn record store")
            session_id = require_admitted_session(admission)
            owner = owner_for_admitted_context(admission, session, session_id)
            if turn_id is None:
                turn_id = uuid4().hex
            if (
                type(turn_id) is not str or not turn_id
                or len(turn_id) > 128
            ):
                raise AdmissionDenied(403, "Invalid durable turn id")
            incarnation_id = getattr(session, "incarnation_id", None)
            if type(incarnation_id) is not str or not incarnation_id:
                raise AdmissionDenied(
                    409, "Durable turn recording requires a fenced session lifetime",
                )
            try:
                records.open_turn(
                    owner=owner, session_id=session_id,
                    incarnation_id=incarnation_id, turn_id=turn_id,
                )
            except TurnRecordError as exc:
                raise AdmissionDenied(409, str(exc)) from exc
            durable_records, durable_turn_id = records, turn_id
            # Reuse the admitted owner for event persistence; the
            # translator validates the record ownership per event.
            durable_owner = owner
        else:
            durable_owner = None
        outcome = TurnOutcome()
        stream = stream_composed_text_turn(
            admission, session, self._turns,
            context_owner=context_owner,
            request=request,
            ports=ports,
            chat_handler=chat_handler,
            chat_processor=chat_processor,
            settings=settings,
            approval_store=approval_store,
            persistence=persistence,
            native_terminal_events=not (
                isinstance(settings, TrustedTextTurnSettings) and bool(settings.offered_tools)
            ),
            native_tool_events=not client_rendering,
            native_route_events=not client_rendering,
        )
        events = translate_legacy_turn_events(
            stream, outcome, client_rendering=client_rendering,
            records=durable_records, turn_id=durable_turn_id,
            turn_owner=durable_owner,
        )
        if persistence is not None:
            events = persist_completed_text_events(
                events, outcome, admission=admission, session=session,
                context_owner=context_owner, persistence=persistence,
            )
        return TypedTurnDelivery(events=events, outcome=outcome, turn_id=durable_turn_id)

    def preview_composed_plain_text_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        persistence: TrustedTextPersistence,
    ) -> AsyncGenerator[str, None]:
        """No-HTTP strict-text Web parity; NOT the production chat adapter.

        This preview uses exactly the same composer and original assistant
        writer. Rich media, tool/UI approvals, workspace edits and
        non-persisting responses have no safe Web compatibility contract yet.
        """
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if owner != context_owner:
            raise AdmissionDenied(403, "Web compatibility owner mismatch")
        if (
            not isinstance(request, TextTurnRequest)
            or not isinstance(request.message, str)
            or not request.message.strip()
            or request.attachment_ids != ()
            or not isinstance(settings, TrustedTextTurnSettings)
            or settings.offered_tools or settings.workspace is not None
            or settings.active_document_id is not None
        ):
            raise AdmissionDenied(403, "Unsupported plain-text Web compatibility turn")
        writer = require_trusted_text_persistence(persistence)
        delivery = self.open_composed_text_events(
            admission, session,
            context_owner=context_owner,
            request=request, ports=ports,
            chat_handler=chat_handler,
            chat_processor=chat_processor,
            settings=settings,
            persistence=replace(writer, emit_message_saved_events=True),
        )
        return serialize_plain_text_web_sse(delivery.events)

    def preview_composed_tool_status_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        approval_store: ToolApprovalStore | None = None,
    ) -> AsyncGenerator[str, None]:
        """No-HTTP, non-persisting tool timeline; NOT production Web or UI.

        This first audited status preview supports one native manage_notes
        surface. It cannot mint approval cards, embed tool results, authorize
        actions or acknowledge conversation saves. Any tool requiring a review
        card must fail closed pending full Web parity.
        """
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if owner != context_owner:
            raise AdmissionDenied(403, "Tool preview context owner mismatch")
        if (
            not isinstance(request, (TextTurnRequest, ToolApprovalTurnRequest))
            or (
                isinstance(request, TextTurnRequest) and
                (type(request.message) is not str or not request.message.strip()
                 or request.attachment_ids != ())
            )
            or not isinstance(settings, TrustedTextTurnSettings)
            or settings.offered_tools != frozenset({"manage_notes"})
            or settings.workspace is not None
            or settings.active_document_id is not None
        ):
            raise AdmissionDenied(403, "Unsupported native tool timeline preview")
        store = tool_approval_store if approval_store is None else approval_store
        if not isinstance(store, ToolApprovalStore):
            raise AdmissionDenied(403, "A trusted review store is required")
        delivery = self.open_composed_text_events(
            admission, session,
            context_owner=context_owner,
            request=request, ports=ports,
            chat_handler=chat_handler,
            chat_processor=chat_processor,
            settings=settings,
            approval_store=store,
        )
        return serialize_plain_text_web_sse(
            delivery.events, require_saved_receipt=False,
            tool_status_preview=frozenset({"manage_notes"}),
            approval_card_resolver=lambda approval_id: read_admitted_pending_review(
                admission, session, store, approval_id, context_owner=context_owner,
            ),
        )

    def preview_persisted_native_notes_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        persistence: TrustedTextPersistence,
        approval_store: ToolApprovalStore | None = None,
    ) -> AsyncGenerator[str, None]:
        """Web-format acceptance of a persisted native notes turn; not routed.

        This process-local preview deliberately has no detached run/replay or
        production HTTP selection. Do not expose it until those lifecycles
        and the interactive approval binding have their own acceptance.
        """
        owner, writer, store = _require_persisted_notes_web_profile(
            admission, session,
            context_owner=context_owner, request=request,
            settings=settings, persistence=persistence,
            approval_store=approval_store,
        )
        delivery = self.open_composed_text_events(
            admission, session,
            context_owner=context_owner, request=request, ports=ports,
            chat_handler=chat_handler, chat_processor=chat_processor,
            settings=settings, approval_store=store,
            persistence=replace(writer, emit_message_saved_events=True),
        )
        return serialize_plain_text_web_sse(
            delivery.events,
            require_saved_receipt=True,
            tool_status_preview=frozenset({"manage_notes"}),
            approval_card_resolver=lambda approval_id: read_admitted_pending_review(
                admission, session, store, approval_id, context_owner=context_owner,
            ),
        )


    def start_detached_persisted_notes_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        persistence: TrustedTextPersistence,
        approval_store: ToolApprovalStore | None = None,
    ) -> TurnStream:
        """Detach one admitted persisted notes turn with exact-run Web cursors.

        The guarded notes_v1 HTTP route may select this method; it remains
        disabled by default. The original run manager owns its lifecycle.
        """
        from src import agent_runs

        owner, writer, store = _require_persisted_notes_web_profile(
            admission, session,
            context_owner=context_owner, request=request,
            settings=settings, persistence=persistence,
            approval_store=approval_store,
        )
        if isinstance(request, ToolApprovalTurnRequest):
            original_review = store.peek(request.approval_id)
            if (
                original_review is None
                or original_review.origin_profile != "notes_v1"
            ):
                raise AdmissionDenied(
                    409, "The approval was not issued by this Gateway profile",
                )
        if agent_runs.get_active_run(session.id) is not None:
            raise AdmissionDenied(409, "A session turn is already running")
        delivery = self.open_composed_detached_text_events(
            admission, session, context_owner=owner,
            request=request, ports=ports,
            chat_handler=chat_handler, chat_processor=chat_processor,
            settings=settings, approval_store=store,
            persistence=replace(writer, emit_message_saved_events=True),
            web_native_notes=True,
        )
        return TurnStream(
            run_id=delivery.run_id,
            events=serialize_plain_text_web_sse(
                delivery.events, require_saved_receipt=True,
                replay=True, replay_review=True,
                tool_status_preview=frozenset({"manage_notes"}),
                approval_card_resolver=lambda approval_id: read_admitted_pending_review(
                    admission, session, store, approval_id,
                    context_owner=owner,
                ),
            ),
        )

    def resume_detached_persisted_notes_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        expected_run_id: str,
        after_cursor: str | None = None,
        approval_store: ToolApprovalStore | None = None,
    ) -> TurnStream | None:
        """Reconstruct the original sealed review from the exact run buffer.

        The full typed prefix is validated, but only SSE frames newer than the
        already acknowledged cursor are sent. No model, writer or tool restarts.
        """
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if owner != context_owner:
            raise AdmissionDenied(403, "Persisted notes replay owner mismatch")
        store = tool_approval_store if approval_store is None else approval_store
        if not isinstance(store, ToolApprovalStore):
            raise AdmissionDenied(403, "Invalid persisted review store")
        delivery = self.resume_composed_detached_text_events(
            admission, session, context_owner=owner,
            expected_run_id=expected_run_id, after_cursor=after_cursor,
            web_native_notes=True, replay_from_start=True,
        )
        if delivery is None:
            return None
        # A terminal pending card might already have expired or been retired
        # before a browser subscribes. Preflight before returning HTTP 200;
        # the serializer still rechecks when it actually emits the card.
        from src import agent_runs
        original = agent_runs.get_matching_run(
            session_id, delivery.run_id, mode="typed",
        )
        if original is None or original.web_native_notes is not True:
            return None
        pending_ids = [
            event.payload.get("approval_id")
            for event in tuple(original.buffer)
            if event.type == "approval.requested"
        ]
        if len(pending_ids) > 1:
            raise AdmissionDenied(409, "Invalid pending review history")
        if pending_ids:
            source_review = store.peek(pending_ids[0])
            if (
                source_review is None
                or source_review.origin_profile != "notes_v1"
            ):
                raise AdmissionDenied(409, "Review is not from this Gateway run")
            read_admitted_pending_review(
                admission, session, store, pending_ids[0],
                context_owner=owner,
            )
        return TurnStream(
            run_id=delivery.run_id,
            events=serialize_plain_text_web_sse(
                delivery.events, require_saved_receipt=True,
                replay=True, replay_review=True, after_cursor=after_cursor,
                tool_status_preview=frozenset({"manage_notes"}),
                approval_card_resolver=lambda approval_id: read_admitted_pending_review(
                    admission, session, store, approval_id,
                    context_owner=owner,
                ),
            ),
        )

    def stop_detached_persisted_notes_web_sse(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        expected_run_id: str,
    ) -> bool:
        """Stop only an exact owner-admitted, notes-origin detached run."""
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if owner != context_owner:
            raise AdmissionDenied(403, "Persisted notes stop owner mismatch")
        if self._turns.resume_typed(
            session_id, expected_run_id, web_native_notes=True,
        ) is None:
            return False
        return self._turns.stop(session_id, expected_run_id)


    def open_composed_detached_text_events(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        approval_store: ToolApprovalStore | None = None,
        persistence: TrustedTextPersistence | None = None,
        web_strict_text: bool = False,
        web_native_notes: bool = False,
        client_rendering: bool = False,
        records: Any = None,
        turn_id: str | None = None,
    ) -> TypedTurnStream:
        """Run the same direct composer behind the inherited detach/replay manager.

        This is a trusted, process-local operation. Validate the issued owner
        and adapter before replacing any existing run. The legacy manager
        retains stop, replacement ordering, and finite replay semantics.
        """
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if context_owner != owner:
            raise AdmissionDenied(403, "Detached turn context owner mismatch")
        if not isinstance(self._turns, TurnService):
            raise AdmissionDenied(403, "Invalid server turn lifecycle")
        if not isinstance(settings, TrustedTextTurnSettings):
            raise AdmissionDenied(403, "Invalid trusted direct turn settings")
        if (
            type(web_strict_text) is not bool
            or type(web_native_notes) is not bool
            or type(client_rendering) is not bool
            or (web_strict_text and web_native_notes)
        ):
            raise AdmissionDenied(403, "Invalid trusted run origin")
        if not isinstance(request, (TextTurnRequest, ToolApprovalTurnRequest)):
            raise AdmissionDenied(403, "Invalid direct turn request")
        if web_native_notes:
            # This provenance can only mark the restricted trusted profile,
            # including the live, unconsumed review and confirmed origin.
            _require_persisted_notes_web_profile(
                admission, session, context_owner=context_owner,
                request=request, settings=settings,
                persistence=persistence, approval_store=approval_store,
            )
        delivery = self.open_composed_text_events(
            admission, session,
            context_owner=context_owner,
            request=request, ports=ports,
            chat_handler=chat_handler,
            chat_processor=chat_processor,
            settings=settings,
            approval_store=approval_store,
            persistence=persistence,
            client_rendering=client_rendering,
            records=records,
            turn_id=turn_id,
        )
        # start_detached_typed creates the inherited drain task immediately.
        # ContextVars are copied into that task, sealing its approvals as
        # notes-origin without changing unrelated direct/legacy turns.
        with bind_gateway_approval_origin("notes_v1" if web_native_notes else ""):
            return self._turns.start_detached_typed(
                session_id, delivery.events, delivery.outcome,
                web_strict_text=web_strict_text,
                web_native_notes=web_native_notes,
            )

    def resume_composed_detached_text_events(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        expected_run_id: str,
        after_cursor: str | None = None,
        web_strict_text: bool = False,
        web_native_notes: bool = False,
        replay_from_start: bool = False,
    ) -> TypedTurnStream | None:
        """Replay one admitted run without crossing owner, modality or cursor."""
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if context_owner != owner:
            raise AdmissionDenied(403, "Detached turn context owner mismatch")
        if not isinstance(self._turns, TurnService):
            raise AdmissionDenied(403, "Invalid server turn lifecycle")
        return self._turns.resume_typed(
            session_id, expected_run_id, after_cursor=after_cursor,
            web_strict_text=web_strict_text,
            web_native_notes=web_native_notes,
            replay_from_start=replay_from_start,
        )

    def stop_composed_detached_text_turn(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        expected_run_id: str,
    ) -> bool:
        """Stop only the exact admitted typed run; reject SSE runs and stale IDs."""
        session_id = require_admitted_session(admission)
        owner = owner_for_admitted_context(admission, session, session_id)
        if context_owner != owner:
            raise AdmissionDenied(403, "Detached turn context owner mismatch")
        if not isinstance(self._turns, TurnService):
            raise AdmissionDenied(403, "Invalid server turn lifecycle")
        if self._turns.resume_typed(session_id, expected_run_id) is None:
            return False
        return self._turns.stop(session_id, expected_run_id)

    async def stream_composed_text_turn(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        request: TextTurnRequest | ToolApprovalTurnRequest,
        ports: ChatContextPorts,
        chat_handler: Any,
        chat_processor: Any,
        settings: TrustedTextTurnSettings,
        approval_store: ToolApprovalStore | None = None,
        native_terminal_events: bool = False,
        native_tool_events: bool = False,
    ) -> AsyncGenerator[str | CoreNativeEvent, None]:
        """Compose a text-input Agent turn without conversation writes or HTTP."""
        async with aclosing(stream_composed_text_turn(
            admission, session, self._turns,
            context_owner=context_owner,
            request=request,
            ports=ports,
            chat_handler=chat_handler,
            chat_processor=chat_processor,
            settings=settings,
            approval_store=approval_store,
            native_terminal_events=native_terminal_events,
            native_tool_events=native_tool_events,
        )) as stream:
            async for chunk in stream:
                yield chunk

    async def stream_admitted_agent(
        self,
        admission: AdmittedSession,
        session: Any,
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
        **runtime_options: Any,
    ) -> AsyncGenerator[str | CoreNativeEvent, None]:
        """Run the existing Agent Loop behind issued session authority."""
        # Closing the facade must synchronously close the admitted service
        # and its inherited loop before request-scoped contextvars are reset.
        async with aclosing(stream_admitted_agent_turn(
            admission, session, self._turns,
            context_owner=context_owner,
            bridge=bridge,
            endpoint_url=endpoint_url,
            model=model,
            messages=messages,
            authority=authority,
            policy=policy,
            workspace=workspace,
            turn_contract=turn_contract,
            **runtime_options,
        )) as stream:
            async for chunk in stream:
                yield chunk

    def open_stream(
        self,
        admission: AdmittedSession,
        stream: AsyncGenerator[str, None],
        *,
        compare_mode: bool,
        client_runtime_context: Any,
    ) -> TurnDelivery:
        """Choose the server-owned stream lifecycle for an admitted turn."""
        return open_admitted_stream(
            admission, self._turns, stream,
            compare_mode=compare_mode,
            client_runtime_context=client_runtime_context,
        )

    def resolve_routed_contract(
        self,
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
        """Resolve one routed immutable contract for the original admitted turn."""
        return resolve_admitted_routed_contract(
            admission, session,
            context_owner=context_owner,
            request_authority=request_authority,
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

    def prepare_tool_inventory(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        base_policy: ToolPolicy,
        base_disabled_tools: set[str] | frozenset[str],
        message: str,
        plan_mode: bool,
        native_workspace_contract: bool,
    ) -> AdmittedToolInventory:
        """Load the inherited native/MCP schemas under this owner's policy."""
        return prepare_admitted_tool_inventory(
            admission, session,
            context_owner=context_owner,
            base_policy=base_policy,
            base_disabled_tools=base_disabled_tools,
            message=message,
            plan_mode=plan_mode,
            native_workspace_contract=native_workspace_contract,
        )

    def classify_turn_capabilities(
        self,
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
        """Select candidate families from the admitted session and intent."""
        return classify_admitted_turn_capabilities(
            admission, session,
            context_owner=context_owner,
            message=message,
            use_turn_contract=use_turn_contract,
            active_document=active_document,
            workspace_bound=workspace_bound,
            image_attachment=image_attachment,
            explicit_browser_intent=explicit_browser_intent,
            external_discovery_intent=external_discovery_intent,
            private_browser_warm=private_browser_warm,
            contextual_browser_followup_lookup=contextual_browser_followup_lookup,
        )

    def project_runtime_ui_state(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        client_runtime_context: dict[str, Any] | None,
        policy: ToolPolicy,
        use_rag: Any,
        use_research: Any,
        incognito: bool,
    ) -> AdmittedRuntimeUIState:
        """Overwrite client toggle claims with effective admitted policy."""
        return project_admitted_runtime_ui_state(
            admission, session,
            context_owner=context_owner,
            client_runtime_context=client_runtime_context,
            policy=policy,
            use_rag=use_rag,
            use_research=use_research,
            incognito=incognito,
        )

    def preview_initial_web_intent(
        self,
        *,
        message: Any,
        search_enabled: bool,
        workspace_bound: bool,
        local_browser_render_lookup: Callable[[], bool],
    ) -> InitialWebIntent:
        """Classify early Web/browser hints without granting tool authority."""
        return classify_initial_web_intent(
            message=message,
            search_enabled=search_enabled,
            workspace_bound=workspace_bound,
            local_browser_render_lookup=local_browser_render_lookup,
        )

    def preview_initial_agent_route(
        self,
        *,
        message: Any,
        chat_mode: str,
        allow_bash: Any,
        search_enabled: bool,
        explicit_web_intent: bool,
        explicit_private_browser_intent: bool,
        native_workspace_inputs: bool,
    ) -> InitialAgentRoute:
        """Pure pre-admission routing; never returns an executable grant."""
        return select_initial_agent_route(
            message=message,
            chat_mode=chat_mode,
            allow_bash=allow_bash,
            search_enabled=search_enabled,
            explicit_web_intent=explicit_web_intent,
            explicit_private_browser_intent=explicit_private_browser_intent,
            native_workspace_inputs=native_workspace_inputs,
        )

    def reconcile_post_session_route(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        message: Any,
        chat_mode: str,
        auto_escalated: bool,
        tool_intent: ToolIntent | None,
        workspace_agent_intent: bool,
        workspace: str | None,
        allow_bash: Any,
        explicit_browser_intent: bool,
        clean_route_requested: bool,
        recent_private_browser_success_lookup: Callable[[], bool],
        contextual_web_followup_lookup: Callable[[], bool],
        contextual_browser_followup_lookup: Callable[[], bool],
        workspace_from_message_lookup: Callable[[], tuple[str | None, Any]],
    ) -> AdmittedPostSessionRoute:
        """Resolve typed history/workspace mode changes after admission."""
        return reconcile_admitted_post_session_route(
            admission, session,
            context_owner=context_owner,
            message=message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            tool_intent=tool_intent,
            workspace_agent_intent=workspace_agent_intent,
            workspace=workspace,
            allow_bash=allow_bash,
            explicit_browser_intent=explicit_browser_intent,
            clean_route_requested=clean_route_requested,
            recent_private_browser_success_lookup=recent_private_browser_success_lookup,
            contextual_web_followup_lookup=contextual_web_followup_lookup,
            contextual_browser_followup_lookup=contextual_browser_followup_lookup,
            workspace_from_message_lookup=workspace_from_message_lookup,
        )

    def promote_document_edit_route(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        message: Any,
        chat_mode: str,
        auto_escalated: bool,
        active_document: bool,
    ) -> AdmittedDocumentEditRoute:
        """Promote only an owner-checked visible document editing request."""
        return promote_admitted_document_edit_route(
            admission, session,
            context_owner=context_owner,
            message=message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            active_document=active_document,
        )

    def reconcile_web_intent(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        message: str,
        explicit_web_intent: bool,
        personal_store_intent: bool,
        tool_intent_is_web: bool,
        capabilities: frozenset[str],
        clean_route_requested: bool,
        allow_web_search: Any,
        search_enabled: bool,
        explicit_browser_intent: bool,
        local_browser_render_intent: bool,
        exact_url_fetch_intent: bool,
        use_turn_contract: bool,
        full_schema_route: bool,
        workspace_bound: bool,
        base_disabled_tools: set[str] | frozenset[str],
        web_link_followup_lookup: Callable[[], bool],
        contextual_web_followup_lookup: Callable[[], bool],
        recent_web_event_lookup: Callable[[], bool],
        message_denies_lookup: Callable[[], bool],
    ) -> AdmittedWebIntent:
        """Resolve owner-bound Web intent, toggle gates and initial denials."""
        return reconcile_admitted_web_intent(
            admission, session,
            context_owner=context_owner,
            message=message,
            explicit_web_intent=explicit_web_intent,
            personal_store_intent=personal_store_intent,
            tool_intent_is_web=tool_intent_is_web,
            capabilities=capabilities,
            clean_route_requested=clean_route_requested,
            allow_web_search=allow_web_search,
            search_enabled=search_enabled,
            explicit_browser_intent=explicit_browser_intent,
            local_browser_render_intent=local_browser_render_intent,
            exact_url_fetch_intent=exact_url_fetch_intent,
            use_turn_contract=use_turn_contract,
            full_schema_route=full_schema_route,
            workspace_bound=workspace_bound,
            base_disabled_tools=base_disabled_tools,
            web_link_followup_lookup=web_link_followup_lookup,
            contextual_web_followup_lookup=contextual_web_followup_lookup,
            recent_web_event_lookup=recent_web_event_lookup,
            message_denies_lookup=message_denies_lookup,
        )

    def select_tool_intent(
        self,
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
        """Select exact request tools and verified follow-ups under admission."""
        return select_admitted_tool_intent(
            admission, session,
            context_owner=context_owner,
            inventory=inventory,
            message=message,
            capabilities=capabilities,
            active_capabilities=active_capabilities,
            warm_tools=warm_tools,
            recent_web_tool_lookup=recent_web_tool_lookup,
            active_document=active_document,
            contextual_browser_followup=contextual_browser_followup,
            contextual_web_followup=contextual_web_followup,
            active_email_review=active_email_review,
            corrected_browser_target=corrected_browser_target,
        )

    def scope_routed_contract(
        self,
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
        """Apply admitted compact routing and reconcile the final tool policy."""
        return scope_admitted_routed_contract(
            admission, session,
            context_owner=context_owner,
            routed=routed,
            schemas=schemas,
            contract_policy=contract_policy,
            disabled_tools=disabled_tools,
            active_capabilities=active_capabilities,
            selected_tools=selected_tools,
            warm_tools=warm_tools,
            compact_preview=compact_preview,
            native_workspace_contract=native_workspace_contract,
            structured_document_intent=structured_document_intent,
            local_browser_render_intent=local_browser_render_intent,
            experiment_header=experiment_header,
            message=message,
            history=history,
            browser_requested=browser_requested,
        )

    def select_compact_runtime(
        self,
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
        """Select the admitted pre-context runtime without changing grants."""
        return select_admitted_compact_runtime(
            admission, session,
            context_owner=context_owner,
            clean_route_requested=clean_route_requested,
            full_schema_route=full_schema_route,
            exact_tool_approval=exact_tool_approval,
            runtime_surface=runtime_surface,
            agent_mode=agent_mode,
            agent_permitted=agent_permitted,
            image_generation=image_generation,
        )

    def select_contract_scope(
        self,
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
        """Select the admitted late tool-contract eligibility and scope."""
        return select_admitted_contract_scope(
            admission, session,
            context_owner=context_owner,
            client_runtime_context=client_runtime_context,
            workspace_agent_intent=workspace_agent_intent,
            workspace_bound=workspace_bound,
            clean_route_requested=clean_route_requested,
            full_schema_route=full_schema_route,
            exact_tool_approval=exact_tool_approval,
        )

    def finalize_tool_policy(
        self,
        admission: AdmittedSession,
        session: Any,
        *,
        context_owner: str | None,
        base_disabled_tools: frozenset[str] | set[str],
        privileges: dict[str, Any] | None,
        global_disabled_tools: Any,
        browser_tools: frozenset[str],
        last_user_message: Any,
        chat_mode: str,
        auto_escalated: bool,
        workspace_agent_intent: bool,
        use_turn_contract: bool,
        allow_browser_for_web_turn: bool,
        compare_mode: bool,
        plan_mode: bool,
    ) -> FinalTurnToolPolicy:
        """Compose final account/global/mode tool restrictions with admission."""
        return finalize_admitted_turn_tool_policy(
            admission, session,
            context_owner=context_owner,
            base_disabled_tools=base_disabled_tools,
            privileges=privileges,
            global_disabled_tools=global_disabled_tools,
            browser_tools=browser_tools,
            last_user_message=last_user_message,
            chat_mode=chat_mode,
            auto_escalated=auto_escalated,
            workspace_agent_intent=workspace_agent_intent,
            use_turn_contract=use_turn_contract,
            allow_browser_for_web_turn=allow_browser_for_web_turn,
            compare_mode=compare_mode,
            plan_mode=plan_mode,
        )

    def dispatch_post_response(
        self,
        admission: AdmittedSession,
        session: Any,
        session_manager: Any,
        *,
        context_owner: str | None,
        task_runner,
        message: str,
        full_response: str,
        last_metrics: dict | None,
        preferences: dict,
        memory_manager: Any,
        memory_vector: Any,
        webhook_manager: Any,
        tools_blocked: bool,
        tool_approval_continuation: bool = False,
        client_runtime_context: Any = None,
        incognito: bool = False,
        compare_mode: bool = False,
        character_name: str | None = None,
        agent_rounds: int = 0,
        agent_tool_calls: int = 0,
        skills_manager: Any = None,
        extract_skills: bool = True,
    ) -> None:
        """Authorize and dispatch inherited post-response work for this owner."""
        dispatch_admitted_post_response(
            admission, session, session_manager,
            context_owner=context_owner,
            task_runner=task_runner,
            message=message,
            full_response=full_response,
            last_metrics=last_metrics,
            preferences=preferences,
            memory_manager=memory_manager,
            memory_vector=memory_vector,
            webhook_manager=webhook_manager,
            tools_blocked=tools_blocked,
            tool_approval_continuation=tool_approval_continuation,
            client_runtime_context=client_runtime_context,
            incognito=incognito,
            compare_mode=compare_mode,
            character_name=character_name,
            agent_rounds=agent_rounds,
            agent_tool_calls=agent_tool_calls,
            skills_manager=skills_manager,
            extract_skills=extract_skills,
        )

    def start_detached(
        self, admission: AdmittedSession, stream: AsyncGenerator[str, None]
    ) -> TurnStream:
        return self._turns.start_detached(require_admitted_session(admission), stream)

    def resume_active(self, admission: AdmittedSession) -> Optional[TurnStream]:
        return self._turns.resume_active(require_admitted_session(admission))

    def stop(self, admission: AdmittedSession, expected_run_id: Optional[str]) -> bool:
        return self._turns.stop(require_admitted_session(admission), expected_run_id)

    def finish(self, admission: AdmittedSession, expected_run_id: Optional[str]) -> bool:
        return self._turns.finish(require_admitted_session(admission), expected_run_id)

    def is_active(self, admission: AdmittedSession) -> bool:
        return self._turns.is_active(require_admitted_session(admission))


gateway_turns = GatewayTurns(turn_service)
