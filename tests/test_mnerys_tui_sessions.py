"""M4.4 acceptance: session navigation and model selection in the TUI.

The SessionsScreen lists, creates and opens owner-scoped sessions; the
ModelScreen picks a model through the server-validated preference update.
Everything shown comes from the Gateway mock; nothing is fabricated.
"""

import json

import httpx
from textual.app import App
from textual.widgets import Input, Static

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.tui.chat import ChatScreen


def _ok(body, status=200):
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "http://gateway.test"),
    )


def _session_gateway() -> tuple[AsyncGatewayClient, dict]:
    state: dict = {"created": [], "patched": [], "details": []}

    def handler(request):
        path = request.url.path
        method = request.method
        if path == "/api/v1/sessions" and method == "GET":
            return _ok({"sessions": [
                {"id": "s1", "name": "Alpha", "model": "m1", "mode": "chat"},
                {"id": "s2", "name": "Beta", "model": "vendor/big", "mode": "chat"},
            ]})
        if path == "/api/v1/sessions" and method == "POST":
            body = json.loads(request.content)
            state["created"].append(body.get("name"))
            return _ok({"id": "s3", "session_id": "s3", "name": body["name"], "model": "vendor/new"}, 201)
        if path == "/api/v1/models":
            return _ok({"models": [
                {"id": "m1", "host": "a"},
                {"id": "vendor/big", "host": "b"},
                {"id": "vendor/new", "host": "c"},
            ]})
        if path.startswith("/api/v1/sessions/s") and method == "PATCH":
            body = json.loads(request.content)
            state["patched"].append((path.rsplit("/", 1)[-1], body))
            skill = path.rsplit("/", 1)[-1]
            return _ok({"id": skill, "name": "n", "model": body.get("model", "m1")})
        if path.startswith("/api/v1/sessions/s"):
            session_id = path.rsplit("/", 1)[-1]
            state["details"].append(session_id)
            models = {
                "s1": "m1", "s2": "vendor/big", "s3": "vendor/new",
            }
            return _ok({
                "session": {"id": session_id, "name": "n", "model": models.get(session_id, "m1"), "mode": "chat"},
                "history": [{"role": "user", "content": "olá"}],
                "total": 1, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        return _ok({}, 404)

    return AsyncGatewayClient(
        "http://gateway.test", "tok", transport=httpx.MockTransport(handler),
    ), state


class _Harness(App[None]):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self.client, "s1", session_name="Alpha"))


async def _settle(pilot, needle: str, tries=60) -> str:
    texts = ""
    for _ in range(tries):
        await pilot.pause(0.05)
        parts: list[str] = []
        for screen in pilot.app.screen_stack:
            parts += [str(s.render()) for s in screen.query(Static)]
        texts = " | ".join(parts)
        if needle in texts:
            return texts
    raise AssertionError(f"'{needle}' never appeared: {texts}")


async def test_sessions_screen_lists_owner_sessions():
    client, _state = _session_gateway()
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "enter open")
        await _settle(pilot, "Beta")
        await pilot.press("escape")
        await pilot.pause(0.1)


async def test_session_names_with_control_characters_render_inert():
    """Control characters in server session names never reach the widgets."""

    def handler(request):
        path = request.url.path
        if path == "/api/v1/sessions" and request.method == "GET":
            return _ok({"sessions": [
                {"id": "evil", "name": "Bad\x1b[31mRed", "model": "m\x1b[32m1", "mode": "chat"},
            ]})
        if path == "/api/v1/models":
            return _ok({"models": [{"id": "m1"}]})
        if path == "/api/v1/sessions/s1":
            return _ok({
                "session": {"id": "s1", "name": "Alpha", "model": "m1", "mode": "chat"},
                "history": [], "total": 0, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        if path == "/api/v1/sessions/evil":
            return _ok({
                "session": {"id": "evil", "name": "Bad\x1b[31mRed", "model": "m1", "mode": "chat"},
                "history": [], "total": 0, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        return _ok({}, 404)

    client = AsyncGatewayClient(
        "http://gateway.test", "tok", transport=httpx.MockTransport(handler),
    )
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "Bad")
        texts = " | ".join(
            str(s.render())
            for screen in app.screen_stack
            for s in screen.query(Static)
        )
        assert "\x1b" not in texts
        assert "Bad" in texts
        await pilot.press("enter")
        await _settle(pilot, "Bad")
        after = " | ".join(
            str(s.render())
            for screen in app.screen_stack
            for s in screen.query(Static)
        )
        assert "\x1b" not in after


async def test_create_failure_with_control_characters_renders_inert_in_the_placeholder():
    """A rejected create keeps its message visible but never raw controls."""

    def handler(request):
        path = request.url.path
        if path == "/api/v1/sessions" and request.method == "POST":
            return _ok({"detail": "bad\x1b]52;c;QUJDRA==\x1b\\name"}, 400)
        if path == "/api/v1/sessions" and request.method == "GET":
            return _ok({"sessions": [{"id": "s1", "name": "Alpha", "model": "m1", "mode": "chat"}]})
        if path == "/api/v1/sessions/s1":
            return _ok({
                "session": {"id": "s1", "name": "Alpha", "model": "m1", "mode": "chat"},
                "history": [], "total": 0, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        return _ok({}, 404)

    client = AsyncGatewayClient(
        "http://gateway.test", "tok", transport=httpx.MockTransport(handler),
    )
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "enter open")
        await pilot.press("n")
        await pilot.press("N", "a", "m", "e")
        await pilot.press("enter")
        input_box = app.screen.query_one("#new-session-input", Input)
        for _ in range(40):
            await pilot.pause(0.05)
            if "create failed" in input_box.placeholder:
                break
        # The rejection stays visible and readable, with zero control bytes.
        assert "create failed: Gateway error 400:" in input_box.placeholder
        assert "\x1b" not in input_box.placeholder
        assert "bad" in input_box.placeholder and "name" in input_box.placeholder


async def test_open_switches_to_the_selected_session():
    client, state = _session_gateway()
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "enter open")
        await _settle(pilot, "Beta")
        sessions_screen = next(
            s for s in app.screen_stack if type(s).__name__ == "SessionsScreen"
        )
        sessions_screen.list_view.index = 1
        await pilot.press("enter")
        await _settle(pilot, "model vendor/big")
        assert state["details"][-1] == "s2"
        chat = next(
            s for s in pilot.app.screen_stack
            if isinstance(s, ChatScreen)
        )
        assert chat.session_id == "s2"


async def test_create_session_goes_through_the_gateway():
    client, state = _session_gateway()
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "enter open")
        await pilot.press("n")
        await pilot.pause(0.2)
        # The new-session input has focus: type a name and confirm.
        for key in ("N", "o", "v", "o"):
            await pilot.press(key)
        await pilot.press("enter")
        await _settle(pilot, "vendor/new")
        assert "Novo" in state["created"]


async def test_model_change_is_server_validated_and_updates_the_header():
    client, state = _session_gateway()
    app = _Harness(client)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "model m1")
        await pilot.press("ctrl+s")
        await _settle(pilot, "enter open")
        await pilot.press("c")
        await _settle(pilot, "Select the session model")
        model_screen = next(
            s for s in app.screen_stack if type(s).__name__ == "ModelScreen"
        )
        model_screen.list_view.index = 2
        await pilot.press("enter")
        await _settle(pilot, "model vendor/new")
        assert state["patched"][-1] == ("s1", {"model": "vendor/new"})