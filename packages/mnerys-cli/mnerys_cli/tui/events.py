"""Typed turn event tracking for the TUI conversation view.

Pure logic, no UI: the tracker applies each sanitized Gateway event exactly
once by its sequence number, so replayed or stale events can never produce
duplicate visible output, and exposes the rendered conversation state.
"""

from dataclasses import dataclass, field

TERMINAL_STATUSES = frozenset({
    "completed", "failed", "cancelled", "awaiting_approval", "approval_decided",
})

TOOL_EVENT_PREFIX = "tool."
APPROVAL_EVENTS = {"approval.requested", "approval.decided"}


@dataclass
class TurnTracker:
    """One turn's rendered state, updated only by ordered server events."""

    applied_sequence: int = 0
    status: str = "streaming"
    deltas: list[str] = field(default_factory=list)
    final_text: str | None = None
    tool_events: list[tuple[str, dict]] = field(default_factory=list)
    approval_id: str | None = None
    approval_decided: str | None = None
    selected_model: str | None = None
    failure_code: str | None = None

    def apply(self, item: dict) -> str | None:
        """Apply one event; return its kind when it changed state.

        Events at or below the applied cursor are idempotently skipped:
        a reconnecting client replays nothing it already rendered.
        """
        try:
            sequence = int(item.get("sequence", 0))
        except (TypeError, ValueError):
            return None
        kind = str(item.get("kind", ""))
        payload = item.get("payload") or {}
        settled_pending = (
            self.status == "awaiting_approval" and (
                kind == "approval.decided" or (kind == "turn.failed" and payload.get("code") == "interrupted")
            )
        )
        if sequence <= self.applied_sequence or (self.status in TERMINAL_STATUSES and not settled_pending):
            return None
        if kind == "message.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self.deltas.append(text)
        elif kind == "message.final":
            text = payload.get("text")
            if isinstance(text, str):
                self.final_text = text
        elif kind == "model.route.selected":
            selected = payload.get("selected_model")
            if isinstance(selected, str) and selected:
                self.selected_model = selected
        elif kind.startswith(TOOL_EVENT_PREFIX):
            self.tool_events.append((kind, dict(payload)))
        elif kind == "approval.requested":
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, str) and approval_id:
                self.approval_id = approval_id
        elif kind == "approval.decided":
            decision = payload.get("decision")
            if isinstance(decision, str) and decision:
                self.approval_decided = decision
            self.approval_id = None
            if settled_pending:
                self.status = "approval_decided"
        elif kind == "turn.approval_decided":
            self.approval_id = None
            self.status = "approval_decided"
        elif kind == "turn.completed":
            self.status = "completed"
        elif kind == "turn.failed":
            self.status = "failed"
            code = payload.get("code")
            self.failure_code = code if isinstance(code, str) else None
            if code == "interrupted":
                self.approval_id = None
        elif kind == "turn.cancelled":
            self.status = "cancelled"
        elif kind == "turn.awaiting_approval":
            self.status = "awaiting_approval"
            if self.approval_id is None:
                self.approval_id = payload.get("approval_id") or None
        elif kind == "metrics":
            pass
        else:
            # Unknown kinds are ignored, never crash the UI; the sequence
            # still advances so a replay cannot reprocess it.
            pass
        self.applied_sequence = sequence
        return kind

    @property
    def rendered_text(self) -> str:
        """The assistant text so far: the final answer or streamed deltas."""
        if self.final_text is not None:
            return self.final_text
        return "".join(self.deltas)
