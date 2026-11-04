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


def legacy_core_terminal_frame(event: CoreTerminalEvent) -> str:
    """Retain the exact preexisting Web SSE completion bytes when needed."""
    if type(event) is not CoreTerminalEvent or event.type != "turn.completed":
        raise TypeError("Unrecognized Agent Core terminal signal")
    return "data: [DONE]\n\n"


def completed_core_event() -> CoreTerminalEvent:
    """Construct a native terminal event without encoding an SSE frame."""
    return CoreTerminalEvent()
