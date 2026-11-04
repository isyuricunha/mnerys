"""Pure preliminary Chat-to-Agent routing before session admission (M2.1).

The Web adapter cannot present a session admission until the submitted/default
session is resolved and owned. This phase merely predicts whether the legacy
Chat route should use Agent mode. It grants no tool, workspace, session or
browser permission. All real operations subsequently require the issued
Gateway admission and server-side execution policy.
"""

from dataclasses import dataclass
from typing import Any

from src.action_intents import ToolIntent, classify_tool_intent


@dataclass(frozen=True)
class InitialAgentRoute:
    """Pure routing hint without execution or authorization effects."""

    chat_mode: str
    tool_intent: ToolIntent | None
    workspace_agent_intent: bool
    auto_escalated: bool
    allow_bash: Any
    escalation_reason: str | None


def select_initial_agent_route(
    *,
    message: Any,
    chat_mode: str,
    allow_bash: Any,
    search_enabled: bool,
    explicit_web_intent: bool,
    explicit_private_browser_intent: bool,
    native_workspace_inputs: bool,
) -> InitialAgentRoute:
    """Preserve the original early Chat-to-Agent promotion order.

    All inputs are hints for pre-admission routing. Neither an inferred
    workspace nor the old allow_bash="true" compatibility flag can confer
    authority; admission, owner-scoped policy and the dispatcher still
    decide whether tools or paths are usable.
    """
    if not isinstance(chat_mode, str):
        raise TypeError("Chat mode must be a string")
    for flag in (
        search_enabled, explicit_web_intent, explicit_private_browser_intent,
        native_workspace_inputs,
    ):
        if type(flag) is not bool:
            raise TypeError("Invalid preliminary routing flag")

    intent = classify_tool_intent(message) if isinstance(message, str) else None
    workspace_intent = bool(
        (
            intent and intent.needs_tools
            and intent.category in {"shell", "workspace"}
        )
        or native_workspace_inputs
    )
    mode = chat_mode
    escalated = False
    reason = None
    bash_toggle = allow_bash

    if chat_mode == "chat" and intent and intent.needs_tools:
        mode = "agent"
        escalated = True
        reason = "tool_intent"
        if workspace_intent:
            bash_toggle = "true"
    elif chat_mode == "chat" and search_enabled:
        mode = "agent"
        escalated = True
        reason = "search_enabled"
    elif chat_mode == "chat" and explicit_web_intent:
        mode = "agent"
        escalated = True
        reason = "explicit_web_intent"
    elif chat_mode == "chat" and explicit_private_browser_intent:
        mode = "agent"
        escalated = True
        reason = "explicit_private_browser_intent"

    return InitialAgentRoute(
        chat_mode=mode,
        tool_intent=intent,
        workspace_agent_intent=workspace_intent,
        auto_escalated=escalated,
        allow_bash=bash_toggle,
        escalation_reason=reason,
    )
