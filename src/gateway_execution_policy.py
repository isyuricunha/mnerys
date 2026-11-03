"""M2.1 server-owned privilege and final tool-policy composition.

The admitted Gateway validates the original session and context owner before
combining account limits, global denials, Compare/Plan constraints and the
inherited guide-only policy. Web-specific request-intent and model/contract
routing remain on the legacy path. This is not a separate tool dispatcher.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)
from src.tool_security import delegated_credential_blocked_tools, plan_mode_disabled_tools
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.gateway_context_admission import owner_for_admitted_context
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


@dataclass(frozen=True)
class FinalTurnToolPolicy:
    """Composed tool denials plus account-enforced mode restrictions."""

    policy: ToolPolicy
    allow_research: bool
    allow_agent: bool
    chat_mode: str


def finalize_admitted_turn_tool_policy(
    admission: AdmittedSession,
    session: Any,
    *,
    context_owner: str | None,
    base_disabled_tools: frozenset[str] | set[str],
    privileges: Mapping[str, Any] | None,
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
    """Finish existing inherited restrictions under an issued admission.

    The HTTP adapter has already applied request-intent, web, incognito and
    active-email restrictions. The server owns the final account/global/mode
    vetoes and guide-only policy. No client option can undo a deny here.

    Privileges, global_disabled_tools and browser_tools must be resolved
    from server configuration, not untrusted request arguments.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if context_owner != owner:
        raise AdmissionDenied(403, "Tool policy context owner does not match admission")
    if not isinstance(base_disabled_tools, (set, frozenset)):
        raise AdmissionDenied(403, "Invalid trusted tool denylist")
    if not isinstance(browser_tools, frozenset):
        raise AdmissionDenied(403, "Invalid trusted browser tool inventory")
    # Preserve legacy callers that omit mode; the turn router may still
    # independently auto-route, but Compare treats any non-agent mode as Chat.
    if not isinstance(chat_mode, str):
        raise AdmissionDenied(403, "Invalid server-resolved chat mode")
    for flag in (
        auto_escalated, workspace_agent_intent, use_turn_contract,
        allow_browser_for_web_turn, compare_mode, plan_mode,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid server turn policy flag")

    disabled = set(base_disabled_tools)
    limits = execution_limits_for_admission(
        admission, privileges, browser_tools=browser_tools,
    )
    # Account restrictions must be applied after all intent-based tool enables.
    disabled.update(limits.disabled_tools)

    if isinstance(global_disabled_tools, list):
        disabled.update(global_disabled_tools)

    # The light auto-escalation clamp is only for personal-store helpers.
    # File-workspace intent retains shell and file operations when permitted.
    if auto_escalated and not workspace_agent_intent and not use_turn_contract:
        disabled.update({"bash", "python", "read_file", "write_file"})
        if not allow_browser_for_web_turn:
            disabled.update(browser_tools)

    session_name = getattr(session, "name", None)
    if session_name and session_name.startswith("[CMP]"):
        disabled.update({"create_document", "edit_document", "update_document"})

    # Demotion precedes Compare's chat-only clamp, matching the legacy order.
    final_mode = chat_mode if limits.allow_agent else "chat"
    if compare_mode:
        disabled.update({
            "create_document", "edit_document", "update_document",
            "chat_with_model", "create_session", "list_sessions",
            "send_to_session", "pipeline", "manage_session",
            "manage_memory", "list_models", "generate_image", "ui_control",
        })
        if final_mode != "agent":
            disabled.update({
                "bash", "python", "read_file", "write_file",
                "web_search", "web_fetch", "search_chats", "manage_tasks",
            })

    if plan_mode:
        disabled.update(plan_mode_disabled_tools())

    return FinalTurnToolPolicy(
        policy=build_effective_tool_policy(
            disabled_tools=disabled,
            last_user_message=last_user_message,
        ),
        allow_research=limits.allow_research,
        allow_agent=limits.allow_agent,
        chat_mode=final_mode,
    )
