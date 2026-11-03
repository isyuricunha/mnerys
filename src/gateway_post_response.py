"""M2.1 admitted boundary for inherited post-response side effects.

The Gateway verifies the issued session and context owner before invoking the
single existing post-response runner. The runner retains its legacy scheduling,
sequential extraction, accounting, webhook and auto-naming behavior.
This is an internal service port, not an independently durable task queue.
"""
from collections.abc import Callable
from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)
from src.gateway_context_admission import owner_for_admitted_context


def post_response_extraction_allowed(
    *,
    tools_blocked: bool,
    tool_approval_continuation: bool,
    client_runtime_context: Any,
) -> bool:
    """Keep unattended and exact-approval continuations free of auxiliary LLM work."""
    context = (
        client_runtime_context
        if isinstance(client_runtime_context, dict)
        else {}
    )
    return bool(
        not tools_blocked
        and not tool_approval_continuation
        and context.get("unattended_mode") is not True
    )


def dispatch_admitted_post_response(
    admission: AdmittedSession,
    session: Any,
    session_manager: Any,
    *,
    context_owner: str | None,
    task_runner: Callable[..., None],
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
    """Allow post-response work only for the original server-admitted owner.

    This service cannot generate a permission from a client runtime hint.
    Denying auxiliary extraction must not suppress token accounting, permitted
    webhooks or auto-naming in the inherited runner.
    """
    session_id = require_admitted_session(admission)
    owner = owner_for_admitted_context(
        admission, session, session_id,
    )
    if context_owner != owner:
        raise AdmissionDenied(403, "Post-response context owner does not match admission")
    if not callable(task_runner):
        raise AdmissionDenied(403, "Trusted post-response runner is required")
    for flag in (
        tools_blocked, tool_approval_continuation, incognito,
        compare_mode, extract_skills,
    ):
        if type(flag) is not bool:
            raise AdmissionDenied(403, "Invalid trusted post-response flag")

    task_runner(
        session, session_manager, session_id,
        message, full_response, last_metrics,
        preferences, memory_manager, memory_vector, webhook_manager,
        incognito=incognito,
        compare_mode=compare_mode,
        character_name=character_name,
        agent_rounds=agent_rounds,
        agent_tool_calls=agent_tool_calls,
        skills_manager=skills_manager,
        owner=owner,
        extract_skills=extract_skills,
        allow_background_extraction=post_response_extraction_allowed(
            tools_blocked=tools_blocked,
            tool_approval_continuation=tool_approval_continuation,
            client_runtime_context=client_runtime_context,
        ),
    )
