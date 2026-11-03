"""M2.1 owner-bound preparation of the inherited tool-schema inventory.

The Gateway assembles the existing native/MCP schema set and *denies* tools
using server-owned account/runtime policy. A schema is a model interface, not
an executable operation grant. The existing request authority and hardened
tool dispatcher remain the sole source of permission.
"""

from dataclasses import dataclass
import re
from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)
from src.gateway_context_admission import owner_for_admitted_context
from src.tool_policy import ToolPolicy, build_effective_tool_policy


@dataclass(frozen=True)
class AdmittedToolInventory:
    """Inherited schema inventory and policy after owner/MCP/fixture guards."""

    schemas: tuple[dict, ...]
    owner_blocked: frozenset[str]
    disabled_tools: frozenset[str]
    policy: ToolPolicy


def explicitly_denies_tool_use(text: str) -> bool:
    """Preserve the inherited 'no tools' request classifier exactly."""
    return bool(re.search(
        r"\b(?:no\s+tools?|do\s+not\s+use\s+(?:any\s+)?tools?|"
        r"don'?t\s+use\s+(?:any\s+)?tools?)\b",
        str(text or ""), re.I,
    ))


def prepare_admitted_tool_inventory(
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
    """Assemble one server-authorized native/MCP schema inventory for a turn.

    This is a private in-process handoff: plan_mode, native_workspace_contract
    and base_policy must originate in the trusted Gateway/route policy, not
    untrusted client flags. The admitted owner determines all owner guards.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(admission, session, session_id)
    if owner != context_owner:
        raise AdmissionDenied(403, "Tool inventory owner does not match admission")
    if (
        not isinstance(base_policy, ToolPolicy)
        or not isinstance(base_disabled_tools, (set, frozenset))
        or not isinstance(message, str)
        or type(plan_mode) is not bool
        or type(native_workspace_contract) is not bool
    ):
        raise AdmissionDenied(403, "Invalid trusted tool inventory input")

    # These are the same inherited implementations previously imported by the
    # Web route. Local imports keep optional MCP/runtime dependencies lazy.
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_utils import get_mcp_manager
    from src.tool_security import (
        blocked_tools_for_owner,
        delegated_credential_blocked_tools,
    )
    from src.agent_loop import (
        _load_mcp_disabled_map,
        _workspace_tools_disabled_for_owner,
        _SFT_DISABLED_WORKSPACE_TOOLS,
    )

    # host_shell is an environment-owned bridge, never a product Web schema.
    schemas = [
        schema for schema in FUNCTION_TOOL_SCHEMAS
        if schema["function"]["name"] != "host_shell"
    ]
    manager = get_mcp_manager()
    owner_blocked = set(blocked_tools_for_owner(owner))
    if admission.principal.delegated:
        owner_blocked.update(delegated_credential_blocked_tools())

    disabled = set(base_disabled_tools) | base_policy.all_disabled_names()
    if _workspace_tools_disabled_for_owner(owner) and not native_workspace_contract:
        # Preserve the SFT Web filesystem guard; native workspace exceptions
        # still require the earlier admission-bound scope selection.
        disabled.update(_SFT_DISABLED_WORKSPACE_TOOLS)

    if manager and not plan_mode and not base_policy.disable_mcp and not owner_blocked:
        schemas.extend(manager.get_all_openai_schemas(_load_mcp_disabled_map()))

    if explicitly_denies_tool_use(message):
        disabled.update(schema["function"]["name"] for schema in schemas)

    policy = build_effective_tool_policy(
        disabled_tools=disabled | owner_blocked,
        last_user_message=message,
    )
    if (
        (base_policy.block_all_tool_calls and not policy.block_all_tool_calls)
        or (base_policy.disable_mcp and not policy.disable_mcp)
    ):
        raise AdmissionDenied(403, "Tool inventory cannot widen original policy")

    return AdmittedToolInventory(
        schemas=tuple(schemas),
        owner_blocked=frozenset(owner_blocked),
        disabled_tools=frozenset(disabled),
        policy=policy,
    )
