"""M4 Web/TUI continuity: one session, two client surfaces, one server truth.

The central M4 acceptance: a conversation written by the Web surface (rows
in the shared message table, as the Web writer persists them) is readable
and continuable from the TUI through the production Gateway router, and a
TUI turn lands in the same durable turn store a Web-side read observes,
without duplicated messages or fabricated events.

The app under test mounts the REAL v1 router with a real temporary SQLite
database and the real durable record store; the fixture composer supplies
the synthetic stream. No real user sessions are used.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from textual.app import App

from core.database import GatewayTurn
from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.tui.chat import ChatInput, ChatScreen
from mnerys_cli.tui.widgets import AssistantMessage, UserMessage
from tests.remote_gateway.v1_app_router import OWNER, build_v1_app


@pytest.fixture()
def continuity_app(tmp_path, monkeypatch):
    return build_v1_app(monkeypatch, tmp_path, seed_session_id="web-1")


class _Harness(App[None]):
    def __init__(self, client, session_id):
        super().__init__()
        self.client = client
        self.session_id = session_id

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self.client, self.session_id, session_name="Web conversation"))


def _tui_client(continuity_app) -> AsyncGatewayClient:
    return AsyncGatewayClient(
        "http://gateway.test", "tok",
        transport=httpx.ASGITransport(app=continuity_app),
    )


def _web_read(continuity_app) -> dict:
    """A Web-side read through the same production router."""
    client = TestClient(continuity_app)
    response = client.get(
        "/api/v1/sessions/web-1", headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 200
    return response.json()


async def _settle(pilot, needle: str, tries=60) -> str:
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


def _latest_turn_id(continuity_app) -> str | None:
    with continuity_app.state.test_engine.connect() as connection:
        row = connection.execute(
            GatewayTurn.__table__.select().order_by(
                GatewayTurn.created_at.desc(),
            ),
        ).first()
        return str(row.id) if row is not None else None


def _parse_sse(body: str) -> list[dict]:
    items = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        items.append(json.loads(payload))
    return items


async def test_web_session_is_readable_and_continuable_from_the_tui(continuity_app):
    client = _tui_client(continuity_app)
    app = _Harness(client, "web-1")
    async with app.run_test(size=(110, 40)) as pilot:
        # Web -> TUI: the persisted conversation renders in the TUI.
        await _settle(pilot, "hello from the web")
        await _settle(pilot, "web reply")
        user_messages = [str(w.render()) for w in app.screen.query(UserMessage)]
        assert any("hello from the web" in text for text in user_messages)
        # TUI -> Web: a new turn runs through the real router.
        app.screen.post_message(ChatInput.Submitted("tui follow-up"))
        await _settle(pilot, "tui follow-up")
        await _settle(pilot, "turn completed")
        # The Web-side read observes the exact TUI turn through the same
        # control-plane endpoints: the record finalized and its ordered,
        # sanitized events replay without duplication.
        turn_id = _latest_turn_id(continuity_app)
        assert turn_id is not None
        via_test_client = TestClient(continuity_app)
        snapshot = via_test_client.get(
            f"/api/v1/turns/{turn_id}", headers={"Authorization": "Bearer tok"},
        )
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "completed"
        replay = via_test_client.get(
            f"/api/v1/turns/{turn_id}/events", params={"after_seq": 0},
            headers={"Authorization": "Bearer tok"},
        )
        kinds = [item["kind"] for item in _parse_sse(replay.text)]
        assert "message.delta" in kinds
        assert kinds[-1] == "turn.completed"
        # A fresh Web-side read of the conversation sees the unchanged seeded
        # rows: cross-client turns never duplicate messages or events.
        web_view = _web_read(continuity_app)
        contents = [entry["content"] for entry in web_view["history"]]
        assert contents.count("hello from the web") == 1
        assert contents.count("web reply") == 1


async def test_tui_turn_is_recorded_in_the_durable_store_after_completion(continuity_app):
    client = _tui_client(continuity_app)
    app = _Harness(client, "web-1")
    async with app.run_test(size=(110, 40)) as pilot:
        await _settle(pilot, "web reply")
        app.screen.post_message(ChatInput.Submitted("durable turn"))
        await _settle(pilot, "turn completed")
    # The Web-side control surface observes the exact finalized turn record.
    records = continuity_app.state.gateway_turn_records
    with continuity_app.state.test_engine.connect() as connection:
        rows = connection.execute(
            GatewayTurn.__table__.select().where(
                GatewayTurn.owner == OWNER,
                GatewayTurn.session_id == "web-1",
            ),
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "completed"
    assert records.read_turn(turn_id=rows[0].id, owner=OWNER)["status"] == "completed"