"""M4.3 integration: the TUI streams real turns through the Gateway router.

The ChatScreen runs against the actual production Gateway v1 router mounted
in the loopback fixture (real admission, real detached run manager, real
event replay over in-process ASGI). No provider and no database-backed
persistence: the fixture composer supplies the synthetic stream.
"""

import httpx
import pytest

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.tui.chat import ChatInput, ChatScreen
from textual.app import App


@pytest.fixture()
def fixture_client():
    from tests.remote_gateway import cli_fixture_server as fixture_module

    transport = httpx.ASGITransport(app=fixture_module.app)
    return AsyncGatewayClient(
        "http://gateway.test", "test-token", transport=transport,
    )


class _Harness(App[None]):
    def __init__(self, screen: ChatScreen):
        super().__init__()
        self._chat = screen

    def on_mount(self) -> None:
        self.push_screen(self._chat)


async def _settle(pilot, needle: str, tries=60) -> str:
    from textual.widgets import Static
    from mnerys_cli.tui.widgets import AssistantMessage

    texts = ""
    for _ in range(tries):
        await pilot.pause(0.05)
        parts: list[str] = []
        for screen in pilot.app.screen_stack:
            parts += [str(s.render()) for s in screen.query(Static)]
            parts += [b.current_text for b in screen.query(AssistantMessage)]
        texts = " | ".join(parts)
        if needle in texts:
            return texts
    raise AssertionError(f"'{needle}' never appeared: {texts}")


async def test_tui_streams_a_turn_through_the_real_router(fixture_client):
    app = _Harness(ChatScreen(fixture_client, "session-1", session_name="Acceptance"))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("2+2"))
        await _settle(pilot, "Olá, CLI!")
        await _settle(pilot, "turn completed")


async def test_tui_cancel_reaches_the_real_router(fixture_client):
    app = _Harness(ChatScreen(fixture_client, "session-1", session_name="Acceptance"))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("wait-turn"))
        await _settle(pilot, "turn streaming")
        await pilot.press("escape")
        await _settle(pilot, "turn cancelled")
