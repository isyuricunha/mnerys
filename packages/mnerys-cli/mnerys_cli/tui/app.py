"""The Mnerys Textual application shell (M4.2).

One connection panel, real Gateway discovery data and a help screen. The
application is a client adapter: it renders only what the authenticated
Gateway returns and owns no session, policy or approval state.
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static

from mnerys_cli.version import distribution_version

# Eclipse: the Mnerys visual language adapted to terminals. Dark canvas,
# teal accent, warm off-white text; every color degrades on limited palettes
# through Rich's quantization.
ECLIPSE_CSS = """
$eclipse-canvas: #17191c;
$eclipse-surface: #1c2023;
$eclipse-surface-high: #22272a;
$eclipse-border: #3c4248;
$eclipse-text: #e8e4dc;
$eclipse-muted: #9ba7a9;
$eclipse-teal: #46c2b3;
$eclipse-teal-bright: #87e2d8;

Screen {
    background: $eclipse-canvas;
    color: $eclipse-text;
}

#dashboard {
    height: auto;
    margin: 1 2;
    padding: 1 2;
    background: $eclipse-surface;
    border: round $eclipse-border;
}

#dashboard-title {
    color: $eclipse-teal-bright;
    text-style: bold;
    margin-bottom: 1;
}

#dashboard-detail {
    color: $eclipse-muted;
}

#connection-status {
    margin: 0 2 1 2;
    color: $eclipse-muted;
}

#connection-status.ok { color: $eclipse-teal-bright; }
#connection-status.error { color: #e8836f; text-style: bold; }

HelpScreen {
    align: center middle;
    background: rgba(23, 25, 28, 0.92);
}

#help-panel {
    width: 72;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: $eclipse-surface-high;
    border: round $eclipse-border;
}

#help-panel .help-title {
    color: $eclipse-teal-bright;
    text-style: bold;
    margin-bottom: 1;
}

#help-panel .help-line {
    color: $eclipse-text;
}

#help-panel .help-keys {
    color: $eclipse-teal;
    text-style: bold;
}
"""


class HelpScreen(ModalScreen[None]):
    """Keyboard shortcut discovery; closes with any key or click."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("question_mark", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Label

        with Vertical(id="help-panel"):
            yield Label("Mnerys keyboard shortcuts", id="help-title", markup=True)
            for keys, description in (
                ("q", "exit Mnerys"),
                ("?", "show this help"),
                ("r", "re-check the Gateway connection"),
            ):
                yield Label(
                    f"[class='help-keys']{keys}[/]  [class='help-line']{description}[/]",
                    classes="help-line", markup=True,
                )
            yield Label(
                "[class='help-line']More surfaces arrive with each M4 "
                "submilestone: sessions, conversation, tools and approvals.[/]",
                classes="help-line", markup=True,
            )
            yield Label(
                "[class='help-keys']escape[/]  [class='help-line']close this help[/]",
                classes="help-line", markup=True,
            )

    def action_close(self) -> None:
        self.app.pop_screen()


class MnerysTui(App[None]):
    """The first-party Mnerys terminal experience."""

    TITLE = "Mnerys"
    CSS = ECLIPSE_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, client, endpoint: str, *, profile: str | None = None):
        super().__init__()
        self.client = client
        self.endpoint = endpoint
        self.profile = profile

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        yield Static("", id="connection-status")
        with Vertical(id="dashboard"):
            yield Static("Mnerys", id="dashboard-title", markup=True)
            yield Static("", id="dashboard-detail", markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_focus(None)
        await self.action_refresh()

    async def action_refresh(self) -> None:
        """Re-check the Gateway connection with real discovery data."""
        status = self.query_one("#connection-status", Static)
        detail = self.query_one("#dashboard-detail", Static)
        status.remove_class("ok")
        status.remove_class("error")
        status.update("connecting to the Gateway…")
        detail.update("")
        self.run_worker(self._refresh_connection, exclusive=True, group="connect")

    async def _refresh_connection(self) -> None:
        status = self.query_one("#connection-status", Static)
        detail = self.query_one("#dashboard-detail", Static)
        code, _body = await self.client.health()
        if code != 200:
            status.add_class("error")
            status.update(f"Gateway unreachable (HTTP {code or 'no connection'})")
            return
        try:
            models = await self.client.models()
            agents = await self.client.agents()
        except Exception as exc:  # auth and network errors are user-facing
            status.add_class("error")
            status.update(f"Gateway rejected the connection: {exc}")
            return
        status.add_class("ok")
        status.update(f"connected as {self.profile or 'default'} profile")
        detail.update(
            f"[b]Endpoint[/b] {self.endpoint}\n"
            f"[b]Version[/b] {self._version()}\n"
            f"[b]Models[/b] {len(models)} visible  "
            f"[b]Agents[/b] {', '.join(str(a.get('id')) for a in agents) or 'none'}\n"
            f"[b]Next[/b] sessions and conversation surfaces land with M4.3/M4.4",
        )

    def _version(self) -> str:
        try:
            return distribution_version()
        except Exception:
            return "unavailable"

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
