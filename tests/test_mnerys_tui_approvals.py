"""M4.5 acceptance: tool statuses and approval decisions in the TUI.

Tool events render as human-readable status lines; pending approvals show a
display-only card and the owner's decision goes through the sealed server
endpoint, with the server's outcome rendered truthfully (including
rejections). No local approval authority exists.
"""

import json

import httpx
from textual.app import App
from textual.widgets import Static

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.tui.chat import ChatInput, ChatScreen
from mnerys_cli.tui.widgets import ApprovalBar

APPROVAL_ID = "a" * 43


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


def _gateway(events, decision_status=200, decision_body=None) -> tuple[AsyncGatewayClient, dict]:
    state: dict = {"decisions": [], "previews": 0}

    def handler(request):
        path = request.url.path
        if "approvals" in path and path.endswith("/decision"):
            body = json.loads(request.content)
            state["decisions"].append(body.get("decision"))
            if decision_status >= 400:
                return _ok({"detail": "approval already consumed"}, decision_status)
            return _ok(
                decision_body or {
                    "approval_id": APPROVAL_ID,
                    "decision": body["decision"],
                    "status": "consumed" if body["decision"] == "approve" else "denied",
                },
            )
        if path.endswith(f"/approvals/{APPROVAL_ID}"):
            state["previews"] += 1
            return _ok({
                "kind": "tool_approval",
                "approval_id": APPROVAL_ID,
                "session_id": "s1",
                "question": "Allow this task to continue?",
                "options": [{"label": "Allow", "value": "approve"}],
            })
        if request.method == "POST" and path == "/api/v1/turns":
            return _ok({"turn_id": "t1", "session_id": "s1", "status": "running"}, 201)
        if path.endswith("/events"):
            return _sse(events)
        if path == "/api/v1/sessions/s1":
            return _ok({
                "session": {"id": "s1", "name": "n", "model": "m1", "mode": "chat"},
                "history": [], "total": 0, "offset": 0, "limit": 200,
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
        self.push_screen(ChatScreen(self.client, "s1", session_name="n"))


async def test_restart_recovery_disables_historical_pending_approval():
    events = [
        {"sequence": 1, "kind": "approval.requested", "payload": {"approval_id": APPROVAL_ID}},
        {"sequence": 2, "kind": "turn.awaiting_approval", "payload": {"approval_id": APPROVAL_ID}},
        {"sequence": 3, "kind": "turn.failed", "payload": {"code": "interrupted", "effects": "unknown", "retryable": False}},
    ]
    client, state = _gateway(events)
    app = _Harness(client)
    try:
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause(0.2)
            app.screen.post_message(ChatInput.Submitted("Continue"))
            await _settle(pilot, "interrupted")
            assert app.screen.pending_approval_id is None
            assert app.screen._approval_bar is None
            await app.screen.action_approve()
            assert state["decisions"] == []
    finally:
        await client.close()


async def _settle(pilot, needle: str, tries=60) -> str:
    parts: list[str] = []
    texts = ""
    for _ in range(tries):
        await pilot.pause(0.05)
        parts = []
        for screen in pilot.app.screen_stack:
            parts += [str(s.render()) for s in screen.query(Static)]
        texts = " | ".join(parts)
        if needle in texts:
            return texts
    raise AssertionError(f"'{needle}' never appeared: {texts}")


APPROVAL_EVENTS = [
    {"sequence": 1, "kind": "tool.started", "payload": {"tool": "manage_notes", "round": 1}},
    {"sequence": 2, "kind": "approval.requested", "payload": {"approval_id": APPROVAL_ID}},
    {"sequence": 3, "kind": "turn.awaiting_approval", "payload": {}},
]


async def test_tool_statuses_and_approval_card_render_from_real_events():
    client, state = _gateway(APPROVAL_EVENTS)
    app = _Harness(client)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        screen.post_message(ChatInput.Submitted("run tool"))
        await _settle(pilot, "tool manage_notes started")
        await _settle(pilot, "Allow this task to continue?")
        assert state["previews"] >= 1
        bars = list(screen.query(ApprovalBar))
        assert bars and bars[0].approval_id == APPROVAL_ID


async def test_approve_sends_the_decision_and_renders_the_server_outcome():
    client, state = _gateway(APPROVAL_EVENTS)
    app = _Harness(client)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("run tool"))
        await _settle(pilot, "Allow this task to continue?")
        await pilot.press("a")
        await _settle(pilot, "Approval consumed")
        assert state["decisions"] == ["approve"]


async def test_deny_sends_deny_and_renders_the_outcome():
    client, state = _gateway(APPROVAL_EVENTS)
    app = _Harness(client)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("run tool"))
        await _settle(pilot, "Allow this task to continue?")
        await pilot.press("d")
        await _settle(pilot, "Approval denied")
        assert state["decisions"] == ["deny"]


async def test_replayed_or_expired_decisions_show_the_server_rejection():
    client, state = _gateway(
        APPROVAL_EVENTS, decision_status=409,
        decision_body={"detail": "approval already consumed"},
    )
    app = _Harness(client)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("run tool"))
        await _settle(pilot, "Allow this task to continue?")
        await pilot.press("a")
        await _settle(pilot, "rejected by the Gateway")
        assert state["decisions"] == ["approve"]


async def test_invalid_credentials_surface_the_auth_error():
    def handler(request):
        return _ok({"detail": "invalid token"}, 401)

    client = AsyncGatewayClient(
        "http://gateway.test", "bad",
        transport=httpx.MockTransport(handler),
    )
    app = _Harness(client)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        app.screen.post_message(ChatInput.Submitted("hi"))
        await _settle(pilot, "Gateway rejected the request (401)")
