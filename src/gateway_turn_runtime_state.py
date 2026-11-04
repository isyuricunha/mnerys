"""M2.1 admitted projection of effective turn toggles into runtime context.

The Web transport owns request parsing and validated client runtime metadata;
this service inserts server-derived, post-policy UI toggle facts into the
runtime context passed to the original authority and agent loop. It does not
grant tools, alter a ToolPolicy, execute effects, or persist user data.
"""

from dataclasses import dataclass
from typing import Any

from src.gateway_admission import AdmittedSession, AdmissionDenied, require_admitted_session
from src.gateway_context_admission import owner_for_admitted_context
from src.tool_policy import ToolPolicy


_DOCUMENT_TOOLS = frozenset({
    "manage_documents", "create_document", "edit_document", "update_document",
})


@dataclass(frozen=True)
class AdmittedRuntimeUIState:
    """Validated context copy and policy-derived toggle state."""

    context: dict[str, Any]
    web_ui_state: dict[str, bool]


def project_admitted_runtime_ui_state(
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
    """Replace potentially spoofed client toggle claims after final policy.

    The caller must supply the admitted loaded session and the *final* policy,
    not a preliminary requested-mode denylist. Existing raw request toggles
    preserve legacy string semantics, but cannot override policy-derived
    web/bash/document-editor availability.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Runtime toggle owner does not match admission")
    if (
        client_runtime_context is not None
        and not isinstance(client_runtime_context, dict)
    ):
        raise AdmissionDenied(403, "Invalid validated runtime context")
    if not isinstance(policy, ToolPolicy) or type(incognito) is not bool:
        raise AdmissionDenied(403, "Invalid admitted runtime toggle policy")

    disabled = policy.all_disabled_names()
    ui_state = {
        "web": "web_search" not in disabled,
        "bash": "bash" not in disabled,
        "rag": str(use_rag if use_rag is not None else "true").lower() != "false",
        "research": str(use_research or "").lower() == "true",
        "incognito": incognito,
        "document_editor": not _DOCUMENT_TOOLS.issubset(disabled),
    }

    # Client metadata remains unmodified, but claimed web_ui_state is always
    # overwritten. Tool authority is still derived independently from policy.
    updated = dict(client_runtime_context or {})
    updated["web_ui_state"] = dict(ui_state)
    return AdmittedRuntimeUIState(context=updated, web_ui_state=ui_state)
