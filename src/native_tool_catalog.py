"""Server-owned tool availability for unified Mnerys Agent turns.

Capability availability is distinct from pre-search, tool execution approval,
and the exact RequestAuthority grants. No client-side Search/Shell toggle can
create or revoke a server-authorized tool. This module only composes routing
hints, never grants execution authority.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Only an unambiguous local instruction suppresses unsolicited Web prefetch
# and stale Web follow-up routing. These tokens do NOT grant Bash access.
_LOCAL_COMMAND = re.compile(
    r"(?<![\w/])(?:ls(?:\s+-[A-Za-z]+)?|pwd|grep|rg|"
    r"git\s+(?:status|diff|log|show)|read_file|get_workspace|glob)\b",
    re.IGNORECASE,
)
# 'cat' is also an animal and 'find' is an ordinary English verb; their
# bare occurrence must never suppress Web routing. Require shell-like paths.
_LOCAL_PATH_COMMAND = re.compile(
    r"(?<![\w/])(?:cat|find)\s+(?:[./~]|[\w.-]+\.[A-Za-z0-9_-]+)(?=\s|/|\b)",
    re.IGNORECASE,
)
_EXPLICIT_WEB = re.compile(
    r"https?://|\b(?:web|internet|online|website|browser|browse|"
    r"pesquis(?:a|e|ar)\s+(?:na\s+)?(?:web|internet)|"
    r"search\s+(?:the\s+)?web|google|searxng)\b",
    re.IGNORECASE,
)
# A small warm surface; ToolSearch discovers the remaining authorized catalog.
_WARM_TOOLS = frozenset({
    "tool_search", "bash", "read_file", "ls", "glob", "grep",
    "web_search", "web_fetch", "ask_user",
})


def is_local_tool_request(text: str) -> bool:
    """Routing hint, not executable tool authorization."""
    value = text or ""
    return bool(_LOCAL_COMMAND.search(value) or _LOCAL_PATH_COMMAND.search(value)) and not bool(
        _EXPLICIT_WEB.search(value)
    )


def retry_local_task_after_irrelevant_tool_failures(
    *,
    native_catalog: bool,
    request_text: str,
    consecutive_failed_rounds: int,
    used_recovery: bool,
    tool_events: Iterable[dict],
) -> bool:
    """Allow one bounded local recovery after unrelated failed Web calls.

    This is only a rerouting instruction. It neither retries an uncertain
    state-changing tool nor grants any new authority. Normal denial and the
    existing loop breaker remain in force.
    """
    if (not native_catalog or used_recovery or consecutive_failed_rounds < 2
            or not is_local_tool_request(request_text)):
        return False
    failures = [
        event for event in tool_events
        if isinstance(event, dict)
        and event.get("tool") in {"web_search", "web_fetch"}
        and (event.get("blocked") or event.get("exit_code") != 0)
    ]
    return len(failures) >= 2


def admitted_agent_tool_surface(
    *,
    relevant: Iterable[str] | None,
    forced: Iterable[str] | None,
    approval_selected: Iterable[str] | None,
    approval_tool: str | None,
    permitted: Iterable[str],
    disabled: Iterable[str],
) -> frozenset[str]:
    """Preserve requested, approved and warm discoverable tools, without grants.

    Only tools present in the server's actual request-authority inventory and
    not disabled by the final server policy may be added. Approval continuation
    does not replace the prior surface with Web-only tools.
    """
    eligible = set(permitted) - set(disabled)
    requested = (
        set(relevant or ())
        | set(forced or ())
        | set(approval_selected or ())
        | set(_WARM_TOOLS)
    )
    if approval_tool:
        requested.add(approval_tool)
    return frozenset(requested & eligible)
