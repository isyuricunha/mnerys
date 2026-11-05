"""Strict, server-controlled eligibility for the first real Web/Core routing slice.

Selection is not a grant. Admission, model ownership, account limits, context
and writer validation remain with their original Gateway/Core owners.
Unrecognized Web features must keep the inherited Web route until parity
is demonstrated.
"""

from collections.abc import Mapping
import re


_ALLOWED_INPUT_FIELDS = frozenset({
    "message", "session", "gateway_profile", "mode",
    "no_memory", "use_rag", "use_web", "allow_web_search",
    "active_doc_state",
    "thinking_mode",
})
# The inherited route does not consume all optional JSON fields as form
# values. Reject them rather than silently discarding a requested feature.
_ALLOWED_JSON_INPUT_FIELDS = frozenset({
    "message", "session", "gateway_profile",
})


def _off(value: object) -> bool:
    return value is None or value is False or value == "" or (
        type(value) is str and value.strip().casefold() == "false"
    )


def eligible_strict_web_text_turn(
    *,
    server_enabled: bool,
    form_values: Mapping,
    json_values: Mapping | None,
    message: object,
    chat_mode: str,
    no_memory: bool,
    use_rag: object,
    use_web: object,
    allow_web_search: object,
    thinking_mode: str | None,
    reasoning_effort: str | None,
    temperature_override: float | None,
    selected_endpoint_id: str,
    approved_plan: str,
    preset_id: object,
    active_doc_id: object,
    active_doc_state: str,
    active_email_uid: object,
    selected_note_ids: object,
    attachments: object,
    workspace: object,
    workspace_rejected: object,
    client_runtime_context: object,
    incognito: bool,
    compare_mode: bool,
    plan_mode: bool,
    tool_approval_continuation: bool,
    do_research: bool,
    image_generation_session: bool,
    foreground_fallback_enabled: bool,
    foreground_fallback_models: frozenset[str] = frozenset(),
    external_untrusted_context_seen: bool,
    internal_tool_request: bool,
    agent_permitted: bool,
) -> bool:
    """Admit only explicitly selected, no-tools/no-side-context browser text.

    True means the original Web endpoint may call the shared direct composer.
    It says nothing about request authority, which the Gateway rechecks.
    Feature selection requires both a trusted server switch and an explicit
    client compatibility profile; either independently absent means legacy.
    """
    if type(server_enabled) is not bool or not server_enabled:
        return False
    if not isinstance(form_values, Mapping) or (
        json_values is not None and not isinstance(json_values, Mapping)
    ):
        return False
    if not set(form_values) <= _ALLOWED_INPUT_FIELDS or (
        json_values is not None and (
            not set(json_values) <= _ALLOWED_JSON_INPUT_FIELDS
            or any(
                key in form_values and form_values[key] != value
                for key, value in json_values.items()
            )
        )
    ):
        return False
    profile = form_values.get("gateway_profile") or (
        json_values or {}
    ).get("gateway_profile")
    if type(profile) is not str or profile != "text_v1":
        return False
    if type(message) is not str or not message.strip() or len(message) > 16384:
        return False
    if chat_mode not in {"", "chat"} or no_memory is not True:
        return False
    if type(use_rag) is not str or use_rag.casefold() != "false":
        return False
    if not _off(use_web) or not _off(allow_web_search):
        return False
    if thinking_mode not in {None, "off"} or reasoning_effort is not None:
        return False
    if temperature_override is not None or selected_endpoint_id or approved_plan:
        return False
    if active_doc_state != "none":
        return False
    if any((preset_id, active_doc_id, active_email_uid, selected_note_ids,
            attachments, workspace, workspace_rejected, client_runtime_context)):
        return False
    if (
        type(foreground_fallback_enabled) is not bool
        or type(foreground_fallback_models) is not frozenset
        or len(foreground_fallback_models) > 10
        or any(
            type(model) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}", model) is None
            for model in foreground_fallback_models
        )
        or (foreground_fallback_enabled and not foreground_fallback_models)
        or (not foreground_fallback_enabled and foreground_fallback_models)
    ):
        return False
    if any((
        incognito, compare_mode, plan_mode, tool_approval_continuation,
        do_research, image_generation_session,
        external_untrusted_context_seen, internal_tool_request,
    )):
        return False
    return agent_permitted is True
