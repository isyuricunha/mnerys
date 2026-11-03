"""Internal Gateway-facing authority and run controls for an admitted session.

This facade is not a new HTTP API or a credential verifier. Every operation
requires an AdmittedSession produced by the Gateway session policy. It keeps
Web adapters from calling the unrestricted legacy run manager directly.
"""

from typing import Any, AsyncGenerator, Iterable, Optional

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
