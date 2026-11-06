"""M4.1: the async Gateway client the TUI consumes.

Transport-level tests with httpx MockTransport: every awaited method sends
the documented request and parses the documented response; SSE frames are
consumed incrementally, malformed frames are skipped and auth failures raise
the shared auth error.
"""

import json

import httpx
import pytest

from mnerys_cli.async_client import AsyncGatewayClient
from mnerys_cli.errors import AuthError, MnerysCliError


def _client(handler, token="tok") -> AsyncGatewayClient:
    """An async client wired to one test handler via MockTransport."""
    return AsyncGatewayClient(
        "http://gateway.test", token, transport=httpx.MockTransport(handler),
    )


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


async def test_create_session_sends_owner_hints_and_parses_the_result():
    seen: dict = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content or b"{}")
        return _ok({"id": "s1", "session_id": "s1", "model": "m1", "mode": "chat"}, 201)

    async with _client(handler) as client:
        data = await client.create_session(
            name="CLI", endpoint_id="ep-1", model="vendor/m3-flash",
        )
    assert data["id"] == "s1"
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/sessions"
    assert seen["body"] == {
        "name": "CLI", "endpoint_id": "ep-1", "model": "vendor/m3-flash",
    }


async def test_session_detail_and_update_use_the_documented_routes():
    seen: dict = {}

    def handler(request):
        seen[request.method] = (request.url.path, dict(request.url.params))
        if request.method == "PATCH":
            seen["patch_body"] = json.loads(request.content or b"{}")
            return _ok({"id": "s1", "name": "renamed", "model": "m2"})
        return _ok({
            "session": {"id": "s1", "name": "n", "model": "m1", "mode": "chat"},
            "history": [{"role": "user", "content": "olá"}],
            "total": 1, "offset": 0, "limit": 200,
            "has_more_before": False, "has_more_after": False,
        })

    async with _client(handler) as client:
        detail = await client.session_detail("s1", limit=50, offset=10)
        updated = await client.update_session("s1", name="renamed", model="m2")
    assert detail["history"][0]["content"] == "olá"
    assert seen["GET"] == ("/api/v1/sessions/s1", {"limit": "50", "offset": "10"})
    assert updated["name"] == "renamed"
    assert seen["patch_body"] == {"name": "renamed", "model": "m2"}


async def test_approvals_surface_sends_owner_scoped_references():
    seen: dict = {}

    def handler(request):
        seen[request.url.path] = dict(request.url.params)
        if request.method == "POST":
            seen["decision"] = json.loads(request.content or b"{}")
            return _ok({"approval_id": "a", "decision": "approve", "status": "consumed"})
        if "approvals" in request.url.path and request.url.path.endswith("/approvals"):
            return _ok({"approvals": [{"approval_id": "a", "status": "pending"}]})
        return _ok({"kind": "tool_approval", "approval_id": "a"})

    async with _client(handler) as client:
        items = await client.session_approvals("s1")
        preview = await client.approval_preview("a", "s1")
        decided = await client.decide_approval("a", "s1", "approve")
    assert items[0]["status"] == "pending"
    assert preview["approval_id"] == "a"
    assert decided["status"] == "consumed"
    assert seen["decision"] == {"decision": "approve"}


async def test_turn_events_streams_incrementally_and_stops_at_done():
    def handler(request):
        return _sse([
            {"sequence": 1, "kind": "message.delta", "payload": {"text": "4"}},
            {"sequence": 2, "kind": "turn.completed", "payload": {}},
        ])

    async with _client(handler) as client:
        items = [item async for item in client.turn_events("t1", after_seq=0)]
    assert [item["kind"] for item in items] == ["message.delta", "turn.completed"]
    assert items[0]["payload"]["text"] == "4"


async def test_turn_events_skips_malformed_frames():
    frames = (
        'data: {"sequence":1,"kind":"message.delta","payload":{"text":"ok"}}\n\n'
        "data: not-json\n\n"
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(
            200, text=frames, request=httpx.Request("GET", "http://gateway.test"),
        )

    async with _client(handler) as client:
        items = [item async for item in client.turn_events("t1")]
    assert len(items) == 1
    assert items[0]["sequence"] == 1


async def test_auth_failures_raise_the_shared_auth_error():
    def handler(request):
        return _ok({"detail": "nope"}, 401)

    async with _client(handler, token="bad") as client:
        with pytest.raises(AuthError):
            await client.models()


async def test_transport_failures_become_cli_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        with pytest.raises(MnerysCliError):
            await client.sessions()


async def test_invalid_endpoint_is_rejected_before_any_request():
    with pytest.raises(MnerysCliError):
        AsyncGatewayClient("ftp://gateway.test", "tok")
