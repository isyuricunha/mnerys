"""Internal Gateway-facing authority and run controls for an admitted session.

This facade is not a new HTTP API or a credential verifier. Every operation
requires an AdmittedSession produced by the Gateway session policy. It keeps
Web adapters from calling the unrestricted legacy run manager directly.
"""

from typing import Any, AsyncGenerator, Callable, Iterable, Optional

from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import AdmittedSession, require_admitted_session
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
from src.gateway_approval_decisions import ToolApprovalDecision, consume_admitted_tool_approval
from src.gateway_turn_authority import authority_for_admitted_turn
from src.tool_policy import ToolPolicy
from src.tool_approvals import ExactToolApproval, ToolApprovalStore
from src.turn_service import TurnService, TurnStream, turn_service
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
)


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
    ) -> ToolApprovalDecision:
        """Validate and consume one existing approval under trusted admission."""
        return consume_admitted_tool_approval(
            admission,
            approval_id,
            decision,
            plan_mode=plan_mode,
            internal_tool_request=internal_tool_request,
            store=store,
        )

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
