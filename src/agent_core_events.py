"""Immutable in-process Agent Core observations, never authorization grants.

Native text/terminal mode remains restricted to tools-disabled direct turns.
Separately opted-in admitted runtimes can emit filtered tool metadata and
sealed approval references. These are not durable, replayable event records.
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


@dataclass(frozen=True, slots=True)
class CoreToolEvent:
    """Tool lifecycle metadata without arguments, results or provider IDs."""

    type: Literal["tool.started", "tool.progress", "tool.completed", "tool.failed",
                  "tool.blocked", "tool.awaiting_approval"]
    tool: str | None = None
    round: int | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class CoreApprovalEvent:
    """Opaque sealed approval reference; never an execution grant."""

    type: Literal["approval.requested", "approval.decided"]
    approval_id: str
    decision: Literal["approve", "approve_task", "deny"] | None = None


CoreNativeEvent = (CoreTerminalEvent | CoreTextEvent | CoreMetricsEvent
                   | CoreFailureEvent | CoreToolEvent | CoreApprovalEvent)


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
