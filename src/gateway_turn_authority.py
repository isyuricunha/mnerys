"""Internal M2.1 request-authority construction from trusted session admission.

This is not a public Gateway endpoint. Identity and session ownership are
validated by the existing HTTP adapter before an authority is built.
"""
from collections.abc import Iterable
from typing import Any

from src.agent_runtime.authority import RequestAuthority, create_request_authority
from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_execution_policy import execution_limits_for_admission
from src.tool_policy import ToolPolicy


def authority_for_admitted_turn(
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
) -> RequestAuthority:
    """Bind policy to an admitted owner/session, including delegated limits.

    The internal-tool flag is derived by a trusted adapter, not client content.
    """
    session_id = require_admitted_session(admission)
    if not isinstance(policy, ToolPolicy) or type(internal_tool_request) is not bool:
        raise AdmissionDenied(403, "Invalid server turn authority context")

    owner = admission.principal.owner_id
    if internal_tool_request:
        authority = RequestAuthority.empty(
            owner=owner, session_id=session_id, workspace=workspace,
        ).restrict(policy)
    else:
        authority = create_request_authority(
            request_text,
            owner=owner,
            session_id=session_id,
            workspace=workspace,
            history=history,
            policy=policy,
            client_runtime_context=client_runtime_context,
            active_document=active_document,
            image_attachment=image_attachment,
            capabilities=capabilities,
        )

    limits = execution_limits_for_admission(
        admission, None, browser_tools=frozenset(),
    )
    return authority.restrict(policy, disabled_tools=limits.disabled_tools)
