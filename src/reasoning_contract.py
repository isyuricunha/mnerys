"""M5.4 increment 4: the ONE provider-independent reasoning-intent contract.

User preferences are stored provider-independently (``off``/``low``/
``medium``/``high``/``max`` plus an optional explicit token budget); each
provider adapter translates the preference ONLY into its documented supported
fields. Unsupported values are never fabricated into wire parameters — the
translation falls back to the documented safe form or omits the control, with
an explicit note.

Anthropic (verified against platform.claude.com docs, 2026-11):

* Claude 4.7+ (Opus 5.5, Sonnet 5.5, Sonnet 5, Fable 5.1/5.2, Mythos 5.1/5.2,
  Haiku 5.5): adaptive thinking only — ``thinking: {type: "enabled",
  budget_tokens}`` returns HTTP 400. Effort is ``output_config.effort``.
* Claude 4.6 (Opus 4.6, Sonnet 4.6): adaptive + effort; ``budget_tokens`` is
  deprecated but still accepted — Mnerys sends the adaptive form.
* Claude 4.5 and earlier thinking models: manual extended thinking
  (``thinking: {type: "enabled", budget_tokens: N}``; minimum 1024, must be
  below ``max_tokens``). Effort composes with a budget only on Opus 4.5.
* Adaptive thinking supports forced tool choice; MANUAL mode restricts
  ``tool_choice`` to ``auto``/``none`` (forced tool use returns an error).
* Thinking blocks (including ``redacted_thinking`` and their opaque
  ``signature`` fields) must be passed back COMPLETE and UNMODIFIED when
  returning tool results — never parsed, regenerated, summarized, reordered
  or filtered; the assistant message is echoed exactly as received.

OpenAI: Responses reasoning items are opaque/encrypted state carried in their
documented order (never synthesized from visible text); Chat Completions uses
``reasoning_effort`` where the model documents it and rejects it alongside
tools on the GPT-5.x chat surface (already handled by the existing adapter).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ANTHROPIC_MIN_BUDGET_TOKENS",
    "ReasoningIntent",
    "anthropic_thinking_config",
    "anthropic_forced_tool_choice_rejected",
    "assistant_thinking_blocks",
    "resolve_reasoning_intent",
]

ANTHROPIC_MIN_BUDGET_TOKENS = 1024

# Documented model generations (platform.claude.com, verified 2026-11).
_ANTHROPIC_ADAPTIVE_REQUIRED = (
    "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5-", "claude-sonnet-5",
    "claude-fable-5", "claude-mythos-5", "claude-haiku-5-5", "claude-haiku-5",
    "claude-opus-4-7", "claude-sonnet-4-7",
)
_ANTHROPIC_ADAPTIVE_PREFERRED = (
    "claude-opus-4-6", "claude-sonnet-4-6",
)
_ANTHROPIC_FORCED_TOOL_CHOICE_REJECTED = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2",
    "claude-mythos-5-2", "claude-fable-5-1", "claude-mythos-5-1",
)
# Effort is supported on adaptive-capable models; on the extended-thinking-only
# generation it composes with a budget ONLY on Opus 4.5.
_ANTHROPIC_BUDGET_ONLY_EFFORT = ("claude-opus-4-5",)


def _model_matches(model: str, prefixes: Tuple[str, ...]) -> bool:
    model_id = str(model or "").strip().lower()
    return any(model_id.startswith(prefix) or f"/{prefix}" in model_id for prefix in prefixes)


def _effective_effort(reasoning_effort: Optional[str]) -> Optional[str]:
    value = str(reasoning_effort or "").strip().lower()
    if value in {"", "auto"}:
        return None
    return value if value in {"off", "low", "medium", "high", "max"} else None


@dataclass(frozen=True)
class ReasoningIntent:
    """The resolved, provider-translatable reasoning preference."""

    provider: str
    model: str
    preference: str  # the user's provider-independent choice (or the default)
    thinking: Optional[dict] = None  # the native thinking parameter (when supported)
    effort: Optional[str] = None  # the native effort value (when supported)
    budget_tokens: Optional[int] = None  # the manual budget (legacy generations)
    note: str = ""  # the mismatch/fallback explanation (empty = exact)

    @property
    def supported(self) -> bool:
        return self.thinking is not None or self.effort is not None or self.note != ""


def resolve_reasoning_intent(
    provider: str, model: str, *, preference: Optional[str] = None,
    thinking_mode: Optional[str] = None, reasoning_effort: Optional[str] = None,
) -> ReasoningIntent:
    """Resolve the user's reasoning preference for one provider/model.

    Never invents wire fields: an unsupported combination produces the
    documented safe form with an explanatory note (or omits the control).
    """
    normalized_provider = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    effort = _effective_effort(reasoning_effort)
    mode = str(thinking_mode or "").strip().lower() or None

    if normalized_provider == "anthropic":
        config = anthropic_thinking_config(
            model_id, thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort, max_tokens=None,
        )
        return ReasoningIntent(
            provider="anthropic", model=model_id,
            preference=str(preference or mode or effort or "auto"),
            thinking=config.get("thinking"),
            effort=(config.get("output_config") or {}).get("effort"),
            budget_tokens=(config.get("thinking") or {}).get("budget_tokens"),
            note=config.get("note", ""),
        )
    if normalized_provider == "openai" and effort is not None:
        return ReasoningIntent(
            provider="openai", model=model_id, preference=effort,
            effort=None,  # the request layer applies reasoning_effort itself
            note="" if effort != "max" else (
                "OpenAI documents low/medium/high (plus minimal on some models); "
                "'max' is not a documented Chat Completions reasoning_effort value."
            ),
        )
    return ReasoningIntent(provider=normalized_provider, model=model_id, preference=str(preference or "auto"))


def anthropic_thinking_config(
    model: str, *, thinking_mode: Optional[str] = None,
    reasoning_effort: Optional[str] = None, max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """The native Anthropic thinking/effort configuration for THIS model.

    Returns a dict that may carry ``thinking``, ``output_config`` and a
    ``note`` — built strictly from the documented per-generation contract.
    """
    mode = str(thinking_mode or "").strip().lower()
    effort = _effective_effort(reasoning_effort)

    if mode == "off":
        return {"thinking": {"type": "disabled"}}

    adaptive_required = _model_matches(model, _ANTHROPIC_ADAPTIVE_REQUIRED)
    adaptive_preferred = _model_matches(model, _ANTHROPIC_ADAPTIVE_PREFERRED)

    if adaptive_required:
        config: Dict[str, Any] = {"thinking": {"type": "adaptive"}}
        note_parts = [
            "extended thinking (budget_tokens) is rejected on this model; using adaptive thinking",
        ]
        if effort and effort != "off":
            config["output_config"] = {"effort": effort}
        else:
            note_parts.append("no explicit effort control sent")
        config["note"] = "; ".join(note_parts)
        return config

    if adaptive_preferred:
        config = {"thinking": {"type": "adaptive"}}
        if effort and effort != "off":
            config["output_config"] = {"effort": effort}
        else:
            config["output_config"] = {"effort": "high"} if mode == "on" else None
            config.pop("output_config", None)
        return config

    numeric_budget: Optional[int] = None
    if reasoning_effort and str(reasoning_effort).strip().isdigit():
        numeric_budget = int(str(reasoning_effort).strip())
    if not mode and effort is None and numeric_budget is None:
        # No explicit preference of any kind: leave the provider default
        # (no thinking configuration sent).
        return {}

    # Manual extended thinking (Claude 4.5 and earlier thinking models).
    budget: Optional[int] = numeric_budget
    if budget is not None:
        if budget < ANTHROPIC_MIN_BUDGET_TOKENS:
            budget = ANTHROPIC_MIN_BUDGET_TOKENS
        if max_tokens and budget >= max_tokens:
            budget = max(ANTHROPIC_MIN_BUDGET_TOKENS, max_tokens - 1)
    elif mode in {"on", "adaptive"}:
        budget = min(16000, (max_tokens - 1024)) if max_tokens and max_tokens > 2048 else 8192
    config = {"thinking": {"type": "enabled", "budget_tokens": budget or ANTHROPIC_MIN_BUDGET_TOKENS}}
    if effort and effort != "off" and _model_matches(model, _ANTHROPIC_BUDGET_ONLY_EFFORT):
        config["output_config"] = {"effort": effort}
    return config


def anthropic_forced_tool_choice_rejected(model: str) -> bool:
    """Whether forced tool_choice with thinking is documented as an error."""
    return _model_matches(model, _ANTHROPIC_FORCED_TOOL_CHOICE_REJECTED)


def assistant_thinking_blocks(message: Dict) -> List[Dict]:
    """The assistant message's thinking blocks, VERBATIM (round-trip replay).

    Anthropic requires thinking blocks (including ``redacted_thinking`` and
    their opaque signatures) returned complete and unmodified when a tool
    result follows. This accessor never parses, reorders, summarizes or
    regenerates them — it returns the stored list as received.
    """
    blocks = (message or {}).get("thinking_blocks")
    if isinstance(blocks, list):
        return list(blocks)  # shallow copy; block dicts are passed through untouched
    return []
