"""M4.3 integration: the TUI streams real turns through the Gateway router.

The ChatScreen runs against the actual production Gateway v1 router mounted
in an isolated app with a real temporary SQLite database and the real
durable record store. The router's global seams (principal, model
registry, session factory) are patched scoped to the continuity suite only,
so this module never pollutes other tests: in-process router tests own
their global patches.
"""

import httpx
import pytest

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.tui.chat import ChatInput
from mnerys_cli.tui.widgets import AssistantMessage
from textual.widgets import Static

from tests.test_mnerys_tui_continuity import _Harness
from tests.remote_gateway.v1_app_router import build_v1_app


@pytest.fixture()
def v1_app(tmp_path, monkeypatch):
    return build_v1_app(monkeypatch, tmp_path, seed_session_id="web-1")


@pytest.fixture()
def fixture_client(v1_app) -> AsyncGatewayClient:
    return AsyncGatewayClient(
        "http://gateway.test", "tok",
        transport=httpx.ASGITransport(app=v1_app),
    )


async def _settle(pilot, needle: str, tries=60) -> str:
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
    app = _Harness(fixture_client, "web-1")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("2+2"))
        await _settle(pilot, "2+2")
        await _settle(pilot, "turn completed")


async def test_tui_cancel_reaches_the_real_router(fixture_client):
    app = _Harness(fixture_client, "web-1")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("wait-turn"))
        await _settle(pilot, "turn streaming")
        await pilot.press("escape")
        await _settle(pilot, "turn cancelled")