"""Session navigation and model selection screens (M4.4).

Every fact comes from the authenticated Gateway: the listing, the model
registry and the preference update (server-validated). The screens are
modals on top of the conversation; opening a session replaces the leading
ChatScreen so the current session survives ordinary navigation.
"""

import logging

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView

from mnerys_cli.errors import MnerysCliError
from mnerys_cli.tui.widgets import safe_markup, strip_control

logger = logging.getLogger("mnerys.tui.sessions")

SESSION_KEYS = {
    "enter": "open",
    "n": "new",
    "c": "model",
    "escape": "close",
}


class SessionsScreen(ModalScreen[str]):
    """Owner-scoped session list: open, create or change the model."""

    BINDINGS = [
        Binding("enter", "open", "Open session", show=False),
        Binding("n", "new_session", "New session"),
        Binding("c", "change_model", "Change model"),
        Binding("escape", "close", "Close"),
    ]

    class SessionChosen(Message):
        def __init__(self, session_id: str, name: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.name = name

    def __init__(self, client, current_session_id: str, *, current_model: str | None = None, on_model_changed=None):
        super().__init__()
        self.client = client
        self.current_session_id = current_session_id
        self.current_model = current_model
        self.on_model_changed = on_model_changed
        self._items: list[dict] = []

    def compose(self):
        with Vertical(id="session-panel"):
            yield Label("Sessions", classes="help-title", markup=True)
            yield Input(
                placeholder="New session name (enter to create)",
                id="new-session-input",
            )
            self.list_view = ListView(id="session-list")
            yield self.list_view
            self.notice = Label("", classes="help-line", markup=True)
            yield self.notice
            yield Label(
                "[class='help-line']enter open · n new · c model · escape close[/]",
                classes="help-line", markup=True,
            )

    async def on_mount(self) -> None:
        self.list_view.focus()
        await self.reload()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._create_from_input()

    async def reload(self) -> None:
        try:
            sessions = await self.client.sessions()
        except MnerysCliError as exc:
            self.notice.update(f"[class='error']{safe_markup(exc)}[/]")
            return
        self._items = sessions
        await self.list_view.clear()
        for item in sessions:
            session_id = str(item.get("id") or "")
            name = str(item.get("name") or session_id)
            model = str(item.get("model") or "?")
            mode = str(item.get("mode") or "chat")
            marker = "▸" if session_id == self.current_session_id else " "
            await self.list_view.append(
                ListItem(Label(
                    f"{marker} {safe_markup(name)}  [class='help-line']{safe_markup(model)} / {safe_markup(mode)}[/]",
                    markup=True,
                ), id=f"session-{session_id}"),
            )

    async def _notify(self, text: str) -> None:
        self.notice.update(f"[class='error']{safe_markup(text)}[/]")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.run_worker(self._open_highlighted(), exclusive=True, group="open-session")

    async def action_open(self) -> None:
        await self._open_highlighted()

    async def _open_highlighted(self) -> None:
        if self.app.screen is not self:
            return  # already dismissed; never double-open
        selected = self.list_view.highlighted_child
        item = None
        if selected is not None and hasattr(selected, "id") and selected.id:
            session_id = str(selected.id).removeprefix("session-")
            item = next(
                (s for s in self._items if str(s.get("id")) == session_id), None,
            )
        if item is None:
            return
        self.post_message(self.SessionChosen(
            str(item.get("id")), str(item.get("name") or ""),
        ))
        self.dismiss(str(item.get("id")))

    async def action_new_session(self) -> None:
        name_box = self.query_one("#new-session-input", Input)
        name_box.focus()
        await self._notify("type a name and press enter to create")

    async def _create_from_input(self) -> None:
        name_box = self.query_one("#new-session-input", Input)
        name = name_box.value.strip() or "New session"
        try:
            created = await self.client.create_session(name=name)
        except MnerysCliError as exc:
            name_box.placeholder = f"create failed: {strip_control(exc)}"
            return
        session_id = str(created.get("id") or "")
        if not session_id:
            name_box.placeholder = "create failed: no session id"
            return
        self.post_message(self.SessionChosen(session_id, name))
        self.dismiss(session_id)

    async def action_change_model(self) -> None:
        selected_id = self.current_session_id
        if selected_id is None:
            return
        try:
            models = await self.client.models()
        except MnerysCliError as exc:
            await self._notify(str(exc))
            return
        ids = [str(m.get("id") or "") for m in models if m.get("id")]
        if not ids:
            await self._notify("the Gateway exposes no models to select")
            return
        self.app.push_screen(ModelScreen(
            self.client, selected_id, ids, current=self.current_model,
            on_chosen=self._apply_model,
        ))

    def _apply_model(self, model: str) -> None:
        self.current_model = model
        if self.on_model_changed is not None:
            self.on_model_changed(model)
        self.run_worker(self.reload(), exclusive=True, group="sessions-reload")

    def action_close(self) -> None:
        self.dismiss(None)


class ModelScreen(ModalScreen[str]):
    """Pick one model; the server validates and persists the preference."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, client, session_id: str, model_ids: list[str], *, current: str | None = None, on_chosen=None):
        super().__init__()
        self.client = client
        self.session_id = session_id
        self.model_ids = model_ids
        self.current = current
        self.on_chosen = on_chosen

    def compose(self):
        with Vertical(id="model-panel"):
            yield Label("Select the session model", classes="help-title", markup=True)
            self.list_view = ListView(id="model-list")
            yield self.list_view
            yield Label(
                "[class='help-line']enter pick · escape close (server-validated)[/]",
                classes="help-line", markup=True,
            )

    async def on_mount(self) -> None:
        self.list_view.focus()
        for index, model in enumerate(self.model_ids):
            marker = "▸" if model == self.current else " "
            await self.list_view.append(ListItem(
                Label(f"{marker} {safe_markup(model)}", markup=True),
                id=f"model-{index}",
            ))

    async def action_close(self) -> None:
        self.dismiss(None)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        selected = event.item
        if selected is None or not selected.id:
            return
        try:
            index = int(str(selected.id).removeprefix("model-"))
            model = self.model_ids[index]
        except (ValueError, IndexError):
            return
        try:
            await self.client.update_session(self.session_id, model=model)
        except MnerysCliError as exc:
            logger.warning("model update rejected: %s", exc)
            self.dismiss(None)
            return
        if self.on_chosen is not None:
            self.on_chosen(model)
        self.dismiss(model)