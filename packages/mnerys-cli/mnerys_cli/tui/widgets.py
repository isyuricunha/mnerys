"""Conversation widgets: untrusted text rendered inert, streamed safely.

Every message body is untrusted data. Control characters (including ESC)
are stripped before anything reaches the widget tree, so terminal escape
sequences can never execute; user text renders through a markup-disabled
Static (literal brackets) and assistant text renders through Rich's
Markdown pipeline. Dynamic text interpolated into markup-enabled widgets
is escaped with :func:`safe_markup`.
"""

import re

from rich.markup import escape
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from mnerys_cli.tui.events import TurnTracker

# Strip C0 controls except tab/newline/carriage return, plus DEL. ESC (0x1b)
# is the only way a terminal control sequence can start; it never reaches a
# widget's rendered text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control(value: object) -> str:
    """One display copy of untrusted text with control characters removed."""
    return _CONTROL_RE.sub("", str(value))


def safe_markup(value: object) -> str:
    """Untrusted text prepared for a ``markup=True`` widget."""
    return escape(strip_control(value))


class UserMessage(Static):
    """One user message, rendered literally."""

    def __init__(self, text: str, **kwargs):
        super().__init__(markup=False, **kwargs)
        self.update(strip_control(text))


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
        # The pending card owns the decision keys: it must be able to
        # receive focus while the text composer keeps typing otherwise.
        self.can_focus = True
        self.update(
            f"[b]Approval required[/b]  {'  '.join(safe_markup(part) for part in line_parts)}\n"
            "[class='help-line']a approve · d deny (decision recorded server-side)[/]",
        )

    def set_outcome(self, outcome: str) -> None:
        self.update(f"[b]Approval {safe_markup(outcome)}[/]")


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
        self._markdown.update(strip_control(text))


def tool_line(kind: str, payload: dict) -> str:
    """One human-readable tool status line (safe, bounded, no raw payloads)."""
    tool = payload.get("tool") or "tool"
    tool = safe_markup(tool)
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
    return f"▸ tool {tool} {safe_markup(kind[len('tool.'):])}{suffix}"


def tracker_status_line(tracker: TurnTracker) -> str:
    """The human-readable terminal state of one turn."""
    if tracker.status == "completed":
        return "turn completed"
    if tracker.status == "failed":
        return f"turn failed ({safe_markup(tracker.failure_code or 'unknown')})"
    if tracker.status == "cancelled":
        return "turn cancelled"
    if tracker.status == "awaiting_approval":
        return "turn waits for an approval decision"
    return "turn streaming…"
