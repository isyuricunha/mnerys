"""Native, transport-independent Agent Core terminal signals.

The inherited Agent Loop still emits legacy SSE for most event families.
An explicitly opted-in, tools-disabled local turn can now receive this
immutable completion signal directly from the Agent Core, before an SSE
serializer runs. This is an incremental transition, not the final durable
TurnEvent protocol, a session identifier or an authorization decision.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CoreTerminalEvent:
    """Core-issued completion fact without provider data or client metadata."""

    type: Literal["turn.completed"] = "turn.completed"


@dataclass(frozen=True, slots=True)
class CoreTextEvent:
    """Visible, completion-gated answer text; not a tool/reasoning payload."""

    type: Literal["message.delta", "message.final"]
    text: str


@dataclass(frozen=True, slots=True)
class CoreMetricsEvent:
    """Only safe numerical usage counts from the final Core metrics."""

    type: Literal["metrics"] = "metrics"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CoreFailureEvent:
    """Non-sensitive terminal failure; no provider body or internal trace."""

    type: Literal["turn.failed"] = "turn.failed"
    code: Literal["provider_error", "agent_terminal"] = "provider_error"
    status: int | None = None


CoreNativeEvent = CoreTerminalEvent | CoreTextEvent | CoreMetricsEvent | CoreFailureEvent


def core_metrics_event(data: dict) -> CoreMetricsEvent:
    """Expose only nonnegative integer counters from trusted Core metrics."""
    def valid(name: str) -> int | None:
        value = data.get(name)
        return value if type(value) is int and value >= 0 else None

    return CoreMetricsEvent(
        input_tokens=valid("input_tokens"),
        output_tokens=valid("output_tokens"),
        total_tokens=valid("total_tokens"),
    )


def core_failure_event(code: str, status: object = None) -> CoreFailureEvent:
    """Validate failure family and status before constructing any event."""
    if code not in {"provider_error", "agent_terminal"}:
        raise ValueError("Unsupported Core failure type")
    safe_status = status if type(status) is int and 400 <= status <= 599 else None
    return CoreFailureEvent(code=code, status=safe_status)


def legacy_core_terminal_frame(event: CoreTerminalEvent) -> str:
    """Retain the exact preexisting Web SSE completion bytes when needed."""
    if type(event) is not CoreTerminalEvent or event.type != "turn.completed":
        raise TypeError("Unrecognized Agent Core terminal signal")
    return "data: [DONE]\n\n"


def completed_core_event() -> CoreTerminalEvent:
    """Construct a native terminal event without encoding an SSE frame."""
    return CoreTerminalEvent()
