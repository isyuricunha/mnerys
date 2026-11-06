"""Conversation widgets: untrusted text rendered inert, streamed safely.

Every message body is untrusted data. User text renders through a
markup-disabled Static (literal brackets, sanitized control characters);
assistant text renders through Rich's Markdown pipeline, which never
executes terminal control sequences. Nothing here ever writes raw bytes to
the terminal outside the widget tree.
"""

from rich.markup import escape
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from mnerys_cli.tui.events import TurnTracker


class UserMessage(Static):
    """One user message, rendered literally."""

    def __init__(self, text: str, **kwargs):
        super().__init__(markup=False, **kwargs)
        self.update(text)


class SystemNotice(Static):
    """One status/notice line (tool activity, errors, connection events)."""

    def __init__(self, text: str, *, tone: str = "muted", **kwargs):
        super().__init__(markup=True, **kwargs)
        self.set_tone(tone)
        self.update(text)

    def set_tone(self, tone: str) -> None:
        for candidate in ("muted", "error", "ok", "pending"):
            self.remove_class(candidate)
        self.add_class(tone)


class ApprovalBar(Static):
    """One human-readable pending approval card; decisions stay server-side.

    The card shows only the server-provided display payload; approving or
    denying submits the owner's decision through the sealed store and never
    reconstructs authority locally.
    """

    def __init__(self, approval_id: str, *line_parts: str, **kwargs):
        super().__init__(markup=True, **kwargs)
        self.approval_id = approval_id
        self.add_class("approval")
        self.update(
            f"[b]Approval required[/b]  {'  '.join(escape(part) for part in line_parts)}\n"
            "[class='help-line']a approve · d deny (decision recorded server-side)[/]",
        )

    def set_outcome(self, outcome: str) -> None:
        self.update(f"[b]Approval {escape(outcome)}[/]")


class AssistantMessage(VerticalScroll):
    """One assistant block: streamed deltas render as live Markdown."""

    DEFAULT_CSS = """
    AssistantMessage { height: auto; max-height: 24; margin-bottom: 1; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_text = ""
        self._markdown = Markdown("")

    def compose(self):
        yield self._markdown

    def set_text(self, text: str) -> None:
        self.current_text = text
        self._markdown.update(text)


def tool_line(kind: str, payload: dict) -> str:
    """One human-readable tool status line (safe, bounded, no raw payloads)."""
    tool = payload.get("tool") or "tool"
    tool = escape(str(tool))
    round_number = payload.get("round")
    suffix = f" (round {round_number})" if isinstance(round_number, int) else ""
    if kind == "tool.started":
        return f"▸ tool {tool} started{suffix}"
    if kind == "tool.progress":
        return f"▸ tool {tool} running{suffix}"
    if kind == "tool.completed":
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return f"▸ tool {tool} finished with exit code {exit_code}{suffix}"
        return f"▸ tool {tool} completed{suffix}"
    if kind == "tool.failed":
        return f"▸ tool {tool} failed{suffix}"
    if kind == "tool.blocked":
        return f"▸ tool {tool} blocked by server policy{suffix}"
    if kind == "tool.awaiting_approval":
        return f"▸ tool {tool} waits for an approval{suffix}"
    return f"▸ tool {tool} {escape(kind[len('tool.'):])}{suffix}"


def tracker_status_line(tracker: TurnTracker) -> str:
    """The human-readable terminal state of one turn."""
    if tracker.status == "completed":
        return "turn completed"
    if tracker.status == "failed":
        return f"turn failed ({escape(tracker.failure_code or 'unknown')})"
    if tracker.status == "cancelled":
        return "turn cancelled"
    if tracker.status == "awaiting_approval":
        return "turn waits for an approval decision"
    return "turn streaming…"
