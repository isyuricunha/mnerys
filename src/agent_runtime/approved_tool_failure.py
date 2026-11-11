"""Truthful summary of a failed sealed approval action for the Web client.

This formats an existing receipt only. It does not retry, broaden authority,
change the approved command, or treat tool output as trusted instructions.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MAX_MESSAGE_CHARS = 4000
_PREEXECUTION_FAILURES = frozenset({
    "request_authority_denied", "resource_identity_denied",
    "turn_contract_denied", "containment_unavailable",
})


def can_continue_after_approved_shell_failure(
    tool: str, command: str, result: Mapping[str, Any],
) -> bool:
    """Only an actually executed, read-only shell failure may resume synthesis.

    The exact action is never retried here. Writable/dynamic shell commands,
    policy denials and missing output must stop at their existing boundary.
    """
    if (
        tool != "bash"
        or type(result.get("exit_code")) is not int
        or result["exit_code"] == 0
        or result.get("error") or result.get("blocked")
        or result.get("approval_required") or result.get("failure_kind")
        or not str(result.get("output") or result.get("stdout") or "").strip()
    ):
        return False
    from src.command_risk import classify_shell_command

    return classify_shell_command(command).is_low_risk


def approved_action_failure_text(tool: str, command: str, result: Mapping[str, Any]) -> str:
    """Describe the action's observed failure, distinguishing launch denial from exit status."""
    name = str(tool or "tool")
    exit_code = result.get("exit_code")
    failure_kind = str(result.get("failure_kind") or "")
    blocked = bool(result.get("blocked") or result.get("approval_required")) or (
        failure_kind in _PREEXECUTION_FAILURES
    )
    if blocked:
        headline = f"The approved {name} action was blocked before execution."
    elif type(exit_code) is int and exit_code != 0:
        headline = f"The approved {name} action returned exit code {exit_code}."
    else:
        headline = f"The approved {name} action reported a failure."

    error = str(result.get("error") or "").strip()
    output = str(
        result.get("output") or result.get("stdout")
        or result.get("response") or result.get("results")
        or result.get("content") or ""
    ).strip()
    detail = []
    if error:
        detail.append(f"Error: {error}")
    if output and output != error:
        detail.append(f"Observed output:\n{output}")
    if (
        not blocked
        and name in {"bash", "host_shell"}
        and type(exit_code) is int and exit_code != 0
        and "&&" in command
    ):
        detail.append(
            "The command uses &&, so steps after the failed command may not have executed."
        )
    message = headline + ("\n\n" + "\n\n".join(detail) if detail else "")
    if len(message) > _MAX_MESSAGE_CHARS:
        return message[:_MAX_MESSAGE_CHARS - len("\n[Output truncated]")] + "\n[Output truncated]"
    return message
