"""M4.2 acceptance: the Textual TUI shell boots and reports real state.

The tests drive the actual application headlessly through Textual's own
``run_test`` pilot: real async workers against a MockTransport Gateway, the
help screen, the error states and the interactive/non-interactive entry
points. Nothing here talks to a real server.
"""

import importlib.util

import httpx
import pytest

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.errors import MnerysCliError
from mnerys_cli.tui.app import MnerysTui


def _ok(body, status=200):
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "http://gateway.test"),
    )


def _client(handler) -> AsyncGatewayClient:
    return AsyncGatewayClient(
        "http://gateway.test", "tok", transport=httpx.MockTransport(handler),
    )


def _healthy_client(models=2, agents=1) -> AsyncGatewayClient:
    def handler(request):
        path = request.url.path
        if path == "/api/health":
            return _ok({"status": "ok"})
        if path == "/api/v1/models":
            return _ok({"models": [
                {"id": "m1", "host": "example.com"},
                {"id": "m2", "host": "example.com"},
            ][:models]})
        if path == "/api/v1/agents":
            return _ok({"agents": [{"id": "chat", "modes": ["chat", "agent"]}] * agents})
        if path == "/api/v1/sessions":
            return _ok({"sessions": [
                {"id": "s1", "name": "Recent", "model": "m1", "mode": "chat"},
            ]})
        if path == "/api/v1/sessions/s1":
            return _ok({
                "session": {"id": "s1", "name": "Recent", "model": "m1", "mode": "chat"},
                "history": [], "total": 0, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        return _ok({}, 404)

    return _client(handler)


async def _settle(pilot, needle: str, tries=40):
    """Pump the app until a text lands on ANY screen (bounded wait)."""
    from textual.widgets import Static

    texts = ""
    for _ in range(tries):
        await pilot.pause(0.05)
        parts: list[str] = []
        for screen in pilot.app.screen_stack:
            parts += [str(static.render()) for static in screen.query(Static)]
        texts = " | ".join(parts)
        if needle in texts:
            return texts
    raise AssertionError(f"'{needle}' never appeared: {texts}")


async def test_headless_boot_reports_a_real_connection():
    app = MnerysTui(_healthy_client(), "http://gateway.test")
    async with app.run_test(size=(100, 30)) as pilot:
        texts = await _settle(pilot, "connected as default profile")
        assert "2 visible" in texts
        assert "http://gateway.test" in texts


async def test_help_screen_opens_and_closes():
    app = MnerysTui(_healthy_client(), "http://gateway.test")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot, "connected as default profile")
        # The default conversation's composer holds the focus, so "?" is a
        # character; help stays reachable through F1.
        await pilot.press("f1")
        await pilot.pause(0.1)
        assert any(type(screen).__name__ == "HelpScreen" for screen in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert all(
            type(screen).__name__ != "HelpScreen" for screen in app.screen_stack
        )


async def test_unreachable_gateway_is_an_explicit_error_state():
    def handler(request):
        raise httpx.ConnectError("boom")

    app = MnerysTui(_client(handler), "http://gateway.test")
    async with app.run_test(size=(100, 30)) as pilot:
        texts = await _settle(pilot, "Gateway unreachable")
        assert "error" in pilot.app.screen.query_one("#connection-status").classes


async def test_rejected_credentials_are_an_explicit_error_state():
    def handler(request):
        if request.url.path == "/api/health":
            return _ok({"status": "ok"})
        return _ok({"detail": "invalid token"}, 401)

    app = MnerysTui(_client(handler), "http://gateway.test")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot, "Gateway rejected the connection")
        assert "error" in pilot.app.screen.query_one("#connection-status").classes


async def test_profile_name_appears_in_the_connection_state():
    app = MnerysTui(
        _healthy_client(), "http://gateway.test", profile="preview",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot, "connected as preview profile")


async def test_quit_binding_exits_cleanly():
    app = MnerysTui(_healthy_client(), "http://gateway.test")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot, "connected as default profile")
        await pilot.press("q")
    assert not app.is_running


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def test_missing_tui_extra_is_an_actionable_error(monkeypatch):
    from mnerys_cli import tui as tui_module

    monkeypatch.setattr(
        tui_module, "find_spec",
        lambda name: None if name == "textual" else importlib.util.find_spec(name),
    )
    with pytest.raises(MnerysCliError) as excinfo:
        tui_module.run_tui("http://gateway.test", "tok")
    assert "mnerys-cli[tui]" in str(excinfo.value)


def test_bare_interactive_entry_launches_the_tui(monkeypatch, tmp_path):
    from mnerys_cli import cli

    launched: dict = {}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("MNERYS_ENDPOINT", "http://gateway.test")
    monkeypatch.setenv("MNERYS_TOKEN", "tok-env")
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)

    def fake_run_tui(endpoint, token, *, profile=None):
        launched["endpoint"] = endpoint
        launched["profile"] = profile
        return 7

    monkeypatch.setattr("mnerys_cli.tui.run_tui", fake_run_tui)
    code = cli.main([])
    assert code == 7
    assert launched == {"endpoint": "http://gateway.test", "profile": None}


def test_explicit_tui_command_passes_the_profile(monkeypatch, tmp_path):
    from mnerys_cli import cli

    launched: dict = {}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("MNERYS_ENDPOINT", "http://gateway.test")
    monkeypatch.setenv("MNERYS_TOKEN", "tok-env")
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)

    def fake_run_tui(endpoint, token, *, profile=None):
        launched["endpoint"] = endpoint
        launched["profile"] = profile
        return 0

    monkeypatch.setattr("mnerys_cli.tui.run_tui", fake_run_tui)
    code = cli.main(["tui", "--profile", "preview"])
    assert code == 0
    assert launched == {"endpoint": "http://gateway.test", "profile": "preview"}


def test_non_tty_entry_keeps_the_classic_help(capsys, monkeypatch):
    from mnerys_cli import cli

    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    code = cli.main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert "tui" in out


def test_tui_without_a_configured_profile_fails_as_a_config_error(
    monkeypatch, tmp_path, capsys,
):
    from mnerys_cli import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("MNERYS_ENDPOINT", raising=False)
    monkeypatch.delenv("MNERYS_TOKEN", raising=False)
    code = cli.main(["tui"])
    assert code == 3
    assert "mnerys connect" in capsys.readouterr().err
