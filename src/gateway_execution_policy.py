"""M1 server-side credential and account privilege limits for agent turns.

A validated AdmittedSession and trusted server-loaded privileges are required.
The inherited request-intent, model, document, global, and tool-contract gates
remain in the current runtime. This module only extracts the restrictions that
previously lived inside the Web agent-route privilege block.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)
from src.tool_security import delegated_credential_blocked_tools
from src.turn_contract import FAMILY_TOOLS


_DOCUMENT_TOOLS = frozenset({
    "manage_documents", "create_document", "edit_document",
    "update_document", "suggest_document",
})
_IMAGE_TOOLS = frozenset({"generate_image", "edit_image"})
_MEMORY_TOOLS = frozenset({"manage_memory", "manage_skills"})
_RESEARCH_TOOLS = frozenset({"trigger_research", "manage_research"})


@dataclass(frozen=True)
class ExecutionPrivilegeLimits:
    """Immutable server-derived denylist and mode restrictions."""

    disabled_tools: frozenset[str]
    allow_research: bool
    allow_agent: bool


def execution_limits_for_admission(
    admission: AdmittedSession,
    privileges: Mapping[str, Any] | None,
    *,
    browser_tools: frozenset[str],
) -> ExecutionPrivilegeLimits:
    """Preserve inherited account privileges, including delegated-token limits.

    The browser-tools inventory and privileges must come from trusted server
    configuration; this is not a client-facing policy construction API.
    """
    require_admitted_session(admission)
    if privileges is None:
        privileges = {}
    if not isinstance(privileges, Mapping):
        raise AdmissionDenied(403, "Invalid server privilege configuration")

    disabled = set()
    if admission.principal.delegated:
        disabled.update(delegated_credential_blocked_tools())

    if privileges:
        if not privileges.get("can_use_bash", True):
            disabled.update(FAMILY_TOOLS["shell_files"])
        if not privileges.get("can_use_browser", True):
            disabled.update(browser_tools)
            disabled.add("private_browser")
        if not privileges.get("can_use_documents", True):
            disabled.update(_DOCUMENT_TOOLS)
        if not privileges.get("can_generate_images", True):
            disabled.update(_IMAGE_TOOLS)
        if not privileges.get("can_manage_memory", True):
            disabled.update(_MEMORY_TOOLS)
        if not privileges.get("can_use_research", True):
            disabled.update(_RESEARCH_TOOLS)

    return ExecutionPrivilegeLimits(
        disabled_tools=frozenset(disabled),
        allow_research=not privileges or bool(privileges.get("can_use_research", True)),
        allow_agent=not privileges or bool(privileges.get("can_use_agent", True)),
    )
