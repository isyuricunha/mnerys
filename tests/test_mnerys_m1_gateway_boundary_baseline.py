"""M1 characterization: inherited Web run endpoints, before Gateway extraction.

These are real FastAPI admission tests over isolated request/DB doubles, plus
an in-memory run-cancellation race. They do NOT assert a durable Gateway,
cross-process replay, or new /api/v1 contract.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI


_SESSION_ID = "m1-test-session"
_RUN_ID = "m1-run"
_RUN_PATHS = (
    ("GET", f"/api/chat/resume/{_SESSION_ID}"),
    ("GET", f"/api/chat/stream_status/{_SESSION_ID}"),
    ("POST", f"/api/chat/stop/{_SESSION_ID}"),
    ("POST", f"/api/chat/finish/{_SESSION_ID}"),
)


@pytest.fixture
def run_app(monkeypatch):
    """Simulate trusted auth middleware, not arbitrary client-supplied identity.

    Auth middleware and token validation are tested separately; this fixture
    supplies their validated request.state identity/scopes to the real routes.
    """
    from routes import chat_routes, session_routes

    monkeypatch.setenv("AUTH_ENABLED", "true")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        owner="alice"
    )
    monkeypatch.setattr(session_routes, "SessionLocal", lambda: db)

    probes = {
        "resume": MagicMock(return_value=SimpleNamespace(run_id=_RUN_ID)),
        "stop": MagicMock(return_value=True),
        "finish": MagicMock(return_value=True),
        "status": MagicMock(return_value=True),
        "subscribe": MagicMock(),
    }

    async def fake_subscribe(*_args, **_kwargs):
        yield "data: [DONE]\n\n"

    probes["subscribe"].side_effect = fake_subscribe
    monkeypatch.setattr(chat_routes.agent_runs, "get_active_run", probes["resume"])
    monkeypatch.setattr(chat_routes.agent_runs, "stop", probes["stop"])
    monkeypatch.setattr(chat_routes.agent_runs, "request_finish", probes["finish"])
    monkeypatch.setattr(chat_routes.agent_runs, "is_active", probes["status"])
    monkeypatch.setattr(chat_routes.agent_runs, "subscribe", probes["subscribe"])

    app = FastAPI()

    @app.middleware("http")
    async def authenticated_fixture(request, call_next):
        # Fixture headers describe the identity already validated by an
        # upstream auth layer. This is NOT a production authentication scheme.
        request.state.api_token = request.headers.get("x-test-transport") == "bearer"
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token_owner = request.headers.get("x-test-owner")
        request.state.api_token_scopes = (
            ["chat"] if request.headers.get("x-test-chat-scope") == "yes" else []
        )
        return await call_next(request)

    app.include_router(chat_routes.setup_chat_routes(*(object() for _ in range(6))))
    return app, probes


def _identity(transport: str, owner: str, *, chat_scope: bool = True):
    if transport == "browser":
        return {"x-test-transport": "browser", "x-test-user": owner}
    return {
        "x-test-transport": "bearer",
        "x-test-user": "api",
        "x-test-owner": owner,
        "x-test-chat-scope": "yes" if chat_scope else "no",
    }


def _assert_no_run_access(probes):
    for operation in probes.values():
        operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _RUN_PATHS)
@pytest.mark.parametrize("transport", ["browser", "bearer"])
async def test_foreign_client_cannot_observe_or_control_session_run(
    run_app, method, path, transport
):
    app, probes = run_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m1.test"
    ) as client:
        response = await client.request(
            method, path,
            headers={**_identity(transport, "bob"), "x-odysseus-run-id": _RUN_ID},
        )
    assert response.status_code == 404
    _assert_no_run_access(probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", _RUN_PATHS)
async def test_bearer_without_chat_scope_is_denied_before_run_access(
    run_app, method, path
):
    app, probes = run_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m1.test"
    ) as client:
        response = await client.request(
            method, path,
            headers={
                **_identity("bearer", "alice", chat_scope=False),
                "x-odysseus-run-id": _RUN_ID,
            },
        )
    assert response.status_code == 403
    _assert_no_run_access(probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["browser", "bearer"])
async def test_owner_clients_can_access_same_run_with_exact_id(run_app, transport):
    app, probes = run_app
    headers = {**_identity(transport, "alice"), "x-odysseus-run-id": _RUN_ID}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m1.test"
    ) as client:
        status = await client.get(
            f"/api/chat/stream_status/{_SESSION_ID}", headers=headers
        )
        resume = await client.get(f"/api/chat/resume/{_SESSION_ID}", headers=headers)
        finish = await client.post(f"/api/chat/finish/{_SESSION_ID}", headers=headers)
        stop = await client.post(f"/api/chat/stop/{_SESSION_ID}", headers=headers)

    assert status.status_code == 200
    assert status.json() == {"status": "streaming", "detached": True}
    assert resume.status_code == 200
    assert resume.headers["x-odysseus-run-id"] == _RUN_ID
    assert "data: [DONE]" in resume.text
    assert finish.json() == {"accepted": True}
    assert stop.json() == {"stopped": True}
    probes["finish"].assert_called_once_with(_SESSION_ID, _RUN_ID)
    probes["stop"].assert_called_once_with(_SESSION_ID, _RUN_ID)


@pytest.mark.asyncio
async def test_replaced_run_rejects_stale_stop_and_only_cancels_exact_active_id():
    """A stale tab/CLI cannot stop a newer in-memory run on the same session."""
    from src import agent_runs

    session_id = "m1-cancel-race"
    old_gate = asyncio.Event()
    new_gate = asyncio.Event()

    async def old_stream():
        await old_gate.wait()
        yield "data: [DONE]\n\n"

    async def new_stream():
        await new_gate.wait()
        yield "data: [DONE]\n\n"

    try:
        old = agent_runs.start(session_id, old_stream())
        await asyncio.sleep(0)
        new = agent_runs.start(session_id, new_stream())
        await asyncio.sleep(0)
        assert old.run_id != new.run_id
        assert agent_runs.get_active_run(session_id) is new
        assert not agent_runs.stop(session_id, old.run_id)
        assert not agent_runs.stop(session_id, None)
        assert not agent_runs.stop(session_id, "")
        assert not new.task.done()
        assert agent_runs.stop(session_id, new.run_id)
        await asyncio.gather(old.task, new.task, return_exceptions=True)
        assert new.status == "stopped"
    finally:
        old_gate.set()
        new_gate.set()
        run = agent_runs._RUNS.pop(session_id, None)
        if run:
            for task in (run.evict_task, run.task):
                if task and not task.done():
                    task.cancel()
