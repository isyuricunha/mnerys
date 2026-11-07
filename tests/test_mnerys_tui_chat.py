"""M4.3 acceptance: the conversation experience.

Unit coverage for the typed-event tracker and the pump (idempotent replay,
reconnection, bounded give-up, clean cancel) plus pilot tests for the
ChatScreen: persisted history, streamed turns, cancellation and untrusted
text rendered inert.
"""

import asyncio
import json

import httpx
import pytest

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.errors import MnerysCliError
from mnerys_cli.tui.chat import ChatInput, ChatScreen
from mnerys_cli.tui.events import TurnTracker
from mnerys_cli.tui.pump import TurnPump
from mnerys_cli.tui.widgets import AssistantMessage, UserMessage, tool_line
from textual.app import App


def _ok(body, status=200):
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "http://gateway.test"),
    )


def _sse(items):
    frames = "".join(
        f"data: {json.dumps(item, separators=(',', ':'))}\n\n" for item in items
    )
    frames += "data: [DONE]\n\n"
    return httpx.Response(
        200, text=frames, request=httpx.Request("GET", "http://gateway.test"),
    )


# ---------------------------------------------------------------------------
# TurnTracker
# ---------------------------------------------------------------------------


def test_tracker_applies_ordered_events_once():
    tracker = TurnTracker()
    assert tracker.apply({
        "sequence": 1, "kind": "message.delta", "payload": {"text": "2+"},
    }) == "message.delta"
    assert tracker.apply({
        "sequence": 2, "kind": "message.delta", "payload": {"text": "2"},
    }) == "message.delta"
    assert tracker.apply({
        "sequence": 3, "kind": "turn.completed", "payload": {},
    }) == "turn.completed"
    assert tracker.rendered_text == "2+2"
    assert tracker.status == "completed"


def test_tracker_replays_are_idempotent():
    tracker = TurnTracker()
    frames = [
        {"sequence": 1, "kind": "message.delta", "payload": {"text": "oi"}},
        {"sequence": 2, "kind": "turn.completed", "payload": {}},
    ]
    for frame in frames:
        tracker.apply(frame)
    for frame in frames:
        assert tracker.apply(frame) is None
    assert tracker.rendered_text == "oi"
    assert tracker.applied_sequence == 2


def test_tracker_final_text_wins_over_deltas():
    tracker = TurnTracker()
    tracker.apply({"sequence": 1, "kind": "message.delta", "payload": {"text": "par"}})
    tracker.apply({
        "sequence": 2, "kind": "message.final", "payload": {"text": "partial answer"},
    })
    assert tracker.rendered_text == "partial answer"


def test_tracker_records_tools_approvals_and_model_route():
    tracker = TurnTracker()
    tracker.apply({
        "sequence": 1, "kind": "model.route.selected",
        "payload": {"selected_model": "vendor/m3-flash"},
    })
    tracker.apply({
        "sequence": 2, "kind": "tool.started", "payload": {"tool": "manage_notes", "round": 1},
    })
    tracker.apply({
        "sequence": 3, "kind": "approval.requested", "payload": {"approval_id": "a" * 43},
    })
    assert tracker.selected_model == "vendor/m3-flash"
    assert tracker.tool_events[0][1]["tool"] == "manage_notes"
    assert tracker.approval_id == "a" * 43
    assert tracker.status == "streaming"
    tracker.apply({
        "sequence": 4, "kind": "turn.awaiting_approval", "payload": {},
    })
    assert tracker.status == "awaiting_approval"


def test_tracker_ignores_garbage_and_unknown_kinds():
    tracker = TurnTracker()
    assert tracker.apply({"sequence": "x", "kind": "junk", "payload": {}}) is None
    assert tracker.apply({"sequence": 1, "kind": "brand.new.kind", "payload": {}}) == "brand.new.kind"
    assert tracker.status == "streaming"
    assert tracker.rendered_text == ""


def test_tool_line_renders_human_readable_statuses():
    assert tool_line("tool.started", {"tool": "manage_notes"}) == "▸ tool manage_notes started"
    assert tool_line("tool.completed", {"tool": "t", "exit_code": 3}) == (
        "▸ tool t finished with exit code 3"
    )
    assert "blocked by server policy" in tool_line("tool.blocked", {"tool": "x"})
    assert tool_line("tool.started", {"tool": "x"}) == "▸ tool x started"


# ---------------------------------------------------------------------------
# TurnPump
# ---------------------------------------------------------------------------


class _ScriptedGateway:
    """A client double scripted per call: results or exceptions in order."""

    def __init__(self, script: list):
        self.script = list(script)
        self.cancel_calls: list[tuple] = []

    async def turn_events(self, turn_id, after_seq=0):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        for frame in item:
            yield frame

    async def turn_snapshot(self, turn_id):
        return {"status": "failed"}

    async def cancel_turn(self, turn_id, session_id):
        self.cancel_calls.append((turn_id, session_id))
        return {"turn_id": turn_id, "status": "cancelled"}


async def test_pump_stops_on_the_terminal_event():
    gateway = _ScriptedGateway([[
        {"sequence": 1, "kind": "message.delta", "payload": {"text": "ok"}},
        {"sequence": 2, "kind": "turn.completed", "payload": {}},
    ]])
    tracker = TurnTracker()
    seen: list[str] = []

    async def record(kind: str) -> None:
        seen.append(kind)

    pump = TurnPump(gateway, "t1", "s1", tracker, record, poll_interval=0.01)
    assert await pump.run() == "completed"
    assert seen == ["message.delta", "turn.completed"]


async def test_pump_reconnects_with_the_same_cursor_and_no_duplicates():
    gateway = _ScriptedGateway([
        MnerysCliError("transport down"),
        [
            {"sequence": 1, "kind": "message.delta", "payload": {"text": "a"}},
            {"sequence": 2, "kind": "turn.completed", "payload": {}},
        ],
    ])
    tracker = TurnTracker()
    pump = TurnPump(
        gateway, "t1", "s1", tracker, lambda _kind: asyncio.sleep(0),
        poll_interval=0.01,
    )
    assert await pump.run() == "completed"
    assert tracker.rendered_text == "a"
    assert tracker.applied_sequence == 2
    # Both scripted rounds were consumed: one transport failure, then the
    # successful replay that completed the turn.
    assert gateway.script == []


async def test_pump_recovers_after_an_aborted_stream():
    """One server-death interruption (converted to MnerysCliError) is
    retried with the same cursor; the turn then completes without gaps."""
    gateway = _ScriptedGateway([
        MnerysCliError("Gateway stream interrupted: <ResponseNotRead ...>"),
        [
            {"sequence": 1, "kind": "message.delta", "payload": {"text": "ok"}},
            {"sequence": 2, "kind": "turn.completed", "payload": {}},
        ],
    ])
    tracker = TurnTracker()
    pump = TurnPump(
        gateway, "t1", "s1", tracker, lambda _kind: asyncio.sleep(0),
        poll_interval=0.01,
    )
    assert await pump.run() == "completed"
    assert tracker.rendered_text == "ok"
    assert tracker.applied_sequence == 2


async def test_pump_gives_up_on_unexpected_errors_without_leaking_them():
    """A raw non-CLI exception must end as the bounded failure, never as an
    unhandled crash escaping the pump."""
    gateway = _ScriptedGateway([ValueError("whoops")] * 6)
    tracker = TurnTracker()
    pump = TurnPump(
        gateway, "t1", "s1", tracker, lambda _kind: asyncio.sleep(0),
        poll_interval=0.01, max_consecutive_failures=5,
    )
    with pytest.raises(MnerysCliError, match="unexpected stream error"):
        await pump.run()
    assert tracker.status == "failed"


async def test_pump_gives_up_after_bounded_failures():
    gateway = _ScriptedGateway([MnerysCliError("down")] * 6)
    tracker = TurnTracker()
    pump = TurnPump(
        gateway, "t1", "s1", tracker, lambda _kind: asyncio.sleep(0),
        poll_interval=0.01, max_consecutive_failures=5,
    )
    with pytest.raises(MnerysCliError):
        await pump.run()


async def test_pump_honors_a_local_cancel_request():
    gateway = _ScriptedGateway([])
    tracker = TurnTracker()
    pump = TurnPump(gateway, "t1", "s1", tracker, lambda _kind: asyncio.sleep(0))
    pump.request_cancel()
    assert await pump.run() == "cancelled"


def _turn_gateway(turns: dict[str, list]) -> tuple[AsyncGatewayClient, dict]:
    """A MockTransport client serving scripted turns and session detail."""
    state: dict = {"cancel_calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/turns":
            body = json.loads(request.content)
            return _ok({
                "turn_id": "t1", "session_id": body["session_id"],
                "status": "running",
            }, 201)
        if path.endswith("/events"):
            return _sse(turns.get("events", []))
        if path.endswith("/cancel"):
            state["cancel_calls"].append(path)
            return _ok({"turn_id": "t1", "status": "cancelled"})
        if path == "/api/v1/sessions/s1":
            return _ok({
                "session": {"id": "s1", "name": "Hist", "model": "m1", "mode": "chat"},
                "history": [
                    {"role": "user", "content": "[green]fake markup[/] and \x1b[31mesc"},
                    {"role": "assistant", "content": "# persisted\nearlier answer"},
                ],
                "total": 2, "offset": 0, "limit": 200,
                "has_more_before": False, "has_more_after": False,
            })
        return _ok({}, 404)

    return AsyncGatewayClient(
        "http://gateway.test", "tok", transport=httpx.MockTransport(handler),
    ), state


async def _settle(pilot, needle: str, tries=60) -> str:
    """Pump the app until a text lands on ANY screen (bounded wait)."""
    from textual.widgets import Static

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


# ---------------------------------------------------------------------------
# ChatScreen
# ---------------------------------------------------------------------------


async def test_history_loads_from_the_gateway_and_renders_untrusted_text():
    client, _state = _turn_gateway({})
    screen = ChatScreen(client, "s1", session_name="Hist")
    app = MnerysTuiHarness(screen)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "persisted")
        user_texts = [
            str(w.render()) for w in app.screen.query(UserMessage)
        ]
        # The literal brackets survive: markup injection renders inert.
        assert any("[green]fake markup[/]" in text for text in user_texts)
        # Control characters are stripped at the widget boundary: the ESC
        # byte never reaches the terminal, and the visible text survives.
        assert all("\x1b" not in text for text in user_texts)
        assert any("esc" in text for text in user_texts)


async def test_streaming_turn_renders_and_finalizes():
    events = [
        {"sequence": 1, "kind": "message.delta", "payload": {"text": "Olá"}},
        {"sequence": 2, "kind": "message.final", "payload": {"text": "Olá, TUI!"}},
        {"sequence": 3, "kind": "turn.completed", "payload": {}},
    ]
    client, _state = _turn_gateway({"events": events})
    screen = ChatScreen(client, "s1", session_name="Hist")
    app = MnerysTuiHarness(screen)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "persisted")
        screen.post_message(ChatInput.Submitted("2+2"))
        await _settle(pilot, "turn completed")
        blocks = list(app.screen.query(AssistantMessage))
        assert blocks, "no assistant block rendered"
        assert any("Olá, TUI!" in block.current_text for block in blocks)


async def test_cancel_binding_requests_the_exact_turn():
    events = [
        {"sequence": 1, "kind": "message.delta", "payload": {"text": "partial"}},
    ]
    client, state = _turn_gateway({"events": events})
    screen = ChatScreen(client, "s1", session_name="Hist")
    app = MnerysTuiHarness(screen)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "persisted")
        screen.post_message(ChatInput.Submitted("long task"))
        await _settle(pilot, "turn streaming")
        await pilot.press("escape")
        await _settle(pilot, "turn cancelled")
        assert state["cancel_calls"], "cancel endpoint was not called"


async def test_multiline_composer_inserts_newlines_and_sends_on_enter():
    client, _state = _turn_gateway({})
    screen = ChatScreen(client, "s1", session_name="Hist")
    app = MnerysTuiHarness(screen)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "persisted")
        composer = app.screen.query_one("#composer", ChatInput)
        composer.focus()
        await pilot.press("l", "i", "n", "e", "1")
        await pilot.press("alt+enter")
        await pilot.press("l", "i", "n", "e", "2")
        assert composer.text == "line1\nline2"
        await pilot.press("enter")
        await _settle(pilot, "line1\nline2")


from textual.app import App


async def test_composer_holds_focus_when_the_conversation_opens():
    client, _state = _turn_gateway({})
    screen = ChatScreen(client, "s1", session_name="Hist")
    app = MnerysTuiHarness(screen)
    async with app.run_test(size=(110, 36)) as pilot:
        await _settle(pilot, "persisted")
        composer = app.screen.query_one(ChatInput)
        assert composer.has_focus
        # Keystrokes land immediately: no extra focus key is needed, and
        # "?" is a character while composing, not a help trigger.
        await pilot.press("k", "?")
        assert composer.text == "k?"


class MnerysTuiHarness(App[None]):
    """A minimal host app that shows one ChatScreen directly."""

    def __init__(self, screen: ChatScreen):
        super().__init__()
        self._chat = screen

    def on_mount(self) -> None:
        self.push_screen(self._chat)
