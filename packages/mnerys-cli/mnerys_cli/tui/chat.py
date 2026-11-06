"""The conversation screen: persisted history, streamed turns, cancellation.

One session at a time. History loads from the owner-scoped Gateway detail
endpoint; every sent message becomes a Gateway turn whose typed events the
pump applies exactly once. All input is untrusted; nothing renders raw.
"""

import logging
import uuid

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import TextArea

from mnerys_cli.errors import MnerysCliError
from mnerys_cli.tui.events import TurnTracker
from mnerys_cli.tui.pump import TurnPump
from mnerys_cli.tui.widgets import (
    AssistantMessage,
    SystemNotice,
    UserMessage,
    safe_markup,
    tracker_status_line,
    tool_line,
)

logger = logging.getLogger("mnerys.tui.chat")


class ChatInput(TextArea):
    """A multiline composer: Enter sends, Alt+Enter inserts a newline."""

    BINDINGS = [
        Binding("enter", "send", "Send", priority=True, show=False),
        Binding("alt+enter", "newline", "Newline", priority=True, show=False),
    ]

    class Submitted(Message):
        """The user finished composing one message."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_send(self) -> None:
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(text))
        self.clear()

    def action_newline(self) -> None:
        self.insert("\n")


class ChatScreen(Screen[None]):
    """One Gateway session's conversation."""

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel turn", priority=True, show=False),
        Binding("ctrl+s", "open_sessions", "Sessions", priority=True, show=False),
        Binding("a", "approve", "Approve", priority=True, show=False),
        Binding("d", "deny", "Deny", priority=True, show=False),
    ]

    def __init__(self, client, session_id: str, *, session_name: str = ""):
        super().__init__()
        self.client = client
        self.session_id = session_id
        self.session_name = session_name
        self.session_model: str | None = None
        self.active_pump: TurnPump | None = None
        self.pending_approval_id: str | None = None
        self._approval_bar = None
        self._sending = False
        self._active_message: AssistantMessage | None = None
        self._active_tracker: TurnTracker | None = None

    def compose(self):
        with Vertical():
            self.header = SystemNotice("", tone="muted")
            yield self.header
            self.conversation = VerticalScroll(id="conversation")
            yield self.conversation
            yield ChatInput(
                id="composer", placeholder="Message the Gateway… (Enter sends, Alt+Enter newline)",
                soft_wrap=True,
            )

    async def on_mount(self) -> None:
        self._update_header()
        await self.reload_history()

    def _update_header(self) -> None:
        name = safe_markup(self.session_name or self.session_id)
        model = safe_markup(self.session_model or "default")
        self.header.update(f"[b]{name}[/]  [class='help-line']model {model} · ctrl+s sessions[/]")

    async def reload_history(self) -> None:
        """Load the persisted conversation from the Gateway (never local)."""
        await self.conversation.remove_children()
        try:
            detail = await self.client.session_detail(self.session_id)
        except MnerysCliError as exc:
            await self.conversation.mount(
                SystemNotice(f"could not load history: {safe_markup(exc)}", tone="error"),
            )
            return
        meta = detail.get("session") or {}
        self.session_model = meta.get("model") or self.session_model
        self._update_header()
        for entry in detail.get("history", []):
            await self._mount_history_entry(entry)
        self.conversation.scroll_end(animate=False, force=True)

    async def action_open_sessions(self) -> None:
        from mnerys_cli.tui.sessions import SessionsScreen

        def on_model_changed(model: str) -> None:
            self.session_model = model
            self._update_header()

        screen = SessionsScreen(
            self.client, self.session_id, current_model=self.session_model,
            on_model_changed=on_model_changed,
        )
        self.app.push_screen(screen, callback=self._on_session_picked)

    async def _on_session_picked(self, selected) -> None:
        if not selected:
            return
        # Replace this conversation with the newly selected session.
        opened = await self.client.session_detail(selected)
        meta = opened.get("session") or {}
        name = str(meta.get("name") or "")
        new_chat = ChatScreen(
            self.client, selected, session_name=name,
        )
        new_chat.session_model = meta.get("model") or None
        self.app.pop_screen()
        await self.app.push_screen(new_chat)

    async def _mount_history_entry(self, entry: dict) -> None:
        role = str(entry.get("role", ""))
        content = entry.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return
        if role == "user":
            await self.conversation.mount(UserMessage(content))
        elif role == "assistant":
            block = AssistantMessage()
            await self.conversation.mount(block)
            block.set_text(content)

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        await self.send_user_message(event.text)

    async def send_user_message(self, text: str) -> None:
        """Start one Gateway turn and stream its typed events.

        The sending flag is held from the guard check until the pump owns
        the turn, so a second Enter during the create-then-mount window can
        never start a duplicate turn.
        """
        if self._sending or self._tracker_is_live():
            await self.conversation.mount(SystemNotice(
                "a turn is already streaming; cancel it first (escape)",
                tone="pending",
            ))
            return
        await self.conversation.mount(UserMessage(text))
        self.conversation.scroll_end(animate=False, force=True)
        self._sending = True
        try:
            created = await self.client.create_turn(
                self.session_id, text, mode="chat",
                idempotency_key=uuid.uuid4().hex,
            )
            turn_id = str(created.get("turn_id") or "")
            if not turn_id:
                await self.conversation.mount(SystemNotice(
                    "the Gateway accepted the turn without a turn id", tone="error",
                ))
                return
            tracker = TurnTracker()
            self._active_tracker = tracker
            block = AssistantMessage()
            self._active_message = block
            await self.conversation.mount(block)
            notice = SystemNotice("turn streaming…", tone="pending")
            await self.conversation.mount(notice)
            self.conversation.scroll_end(animate=False, force=True)

            async def on_event(kind: str) -> None:
                await self._on_turn_event(kind, tracker, block)

            pump = TurnPump(self.client, turn_id, self.session_id, tracker, on_event)
            # The pump now owns the turn; only then may the sending flag drop.
            self.active_pump = pump

            async def finish() -> None:
                try:
                    status = await pump.run()
                except MnerysCliError as exc:
                    notice.set_tone("error")
                    notice.update(f"stream lost: {safe_markup(exc)}")
                    return
                finally:
                    self.active_pump = None
                notice.set_tone("ok" if status == "completed" else "pending")
                notice.update(tracker_status_line(tracker))
                block.set_text(tracker.rendered_text)
                self.conversation.scroll_end(animate=False, force=True)

            self.run_worker(finish, exclusive=True, group=f"turn-{turn_id}")
        except MnerysCliError as exc:
            await self.conversation.mount(SystemNotice(
                safe_markup(exc), tone="error",
            ))
        finally:
            self._sending = False

    async def _on_turn_event(self, kind: str, tracker: TurnTracker, block) -> None:
        if kind.startswith("tool."):
            await self.conversation.mount(SystemNotice(
                tool_line(kind, tracker.tool_events[-1][1]),
            ))
        elif kind == "approval.requested":
            await self._show_approval(tracker.approval_id)
        elif kind == "approval.decided":
            await self._clear_approval()
        elif kind == "turn.awaiting_approval":
            await self._show_approval(tracker.approval_id)
        elif kind == "message.delta":
            block.set_text(tracker.rendered_text)
            self.conversation.scroll_end(animate=False, force=True)
        elif kind == "message.final":
            block.set_text(tracker.rendered_text)

    async def _show_approval(self, approval_id) -> None:
        """Mount one human-readable pending approval card (display only)."""
        from mnerys_cli.tui.widgets import ApprovalBar

        if self.pending_approval_id == approval_id and self._approval_bar is not None:
            return
        approval_id = str(approval_id or "")
        if not approval_id:
            await self.conversation.mount(SystemNotice(
                "the Gateway requested an approval without an id",
                tone="error",
            ))
            return
        self.pending_approval_id = approval_id
        bar = ApprovalBar(approval_id, "the Gateway needs a decision")
        self._approval_bar = bar
        await self.conversation.mount(bar)
        self.conversation.scroll_end(animate=False, force=True)
        try:
            payload = await self.client.approval_preview(approval_id, self.session_id)
            if isinstance(payload, dict) and payload.get("question"):
                bar.update(
                    f"[b]Approval required[/b]  {safe_markup(payload['question'])}\n"
                    "[class='help-line']a approve · d deny (decision recorded server-side)[/]",
                )
        except MnerysCliError:
            pass  # the pending state itself is still true; the card stays

    async def _clear_approval(self) -> None:
        if self._approval_bar is not None:
            self._approval_bar.set_outcome("decided by the Gateway")
        self.pending_approval_id = None
        self._approval_bar = None

    async def action_approve(self) -> None:
        await self._decide("approve")

    async def action_deny(self) -> None:
        await self._decide("deny")

    async def _decide(self, decision: str) -> None:
        """Submit one decision through the sealed server store."""
        approval_id = self.pending_approval_id
        bar = self._approval_bar
        if not approval_id or bar is None:
            return
        self.pending_approval_id = None
        try:
            result = await self.client.decide_approval(
                approval_id, self.session_id, decision,
            )
        except MnerysCliError as exc:
            bar.set_outcome(f"rejected by the Gateway: {safe_markup(exc)}")
            return
        bar.set_outcome(str(result.get("status") or result.get("decision") or "decided"))
        self._approval_bar = None

    def _tracker_is_live(self) -> bool:
        return self.active_pump is not None and not self.active_pump._cancel_requested

    async def action_cancel_turn(self) -> None:
        """Cancel the exact active turn; the server remains the authority.

        A silent no-op when nothing streams, so closing screens and help
        navigation never produce noise.
        """
        pump = self.active_pump
        if pump is None:
            return
        pump.request_cancel()
        try:
            await self.client.cancel_turn(pump.turn_id, pump.session_id)
        except MnerysCliError as exc:
            await self.conversation.mount(SystemNotice(
                f"cancel request failed: {safe_markup(exc)}", tone="error",
            ))
