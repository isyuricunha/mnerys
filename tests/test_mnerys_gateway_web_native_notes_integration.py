"""Actual /api/chat_stream, /resume and /stop route contracts for notes_v1.

Exercises route-owned admission and the original Gateway, composer, approval
store, typed run buffer and writers with synthetic model/tool observations.
No external provider, production DB, or deployment is involved.
"""

import asyncio
from types import SimpleNamespace

from core.models import ChatMessage
from fastapi import HTTPException
import pytest

import routes.chat_routes as chat_routes
import routes.gateway_notes_adapter as notes_adapter
from src import agent_runs
from src import turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_foreground_model_routing import _RouteRequest
from tests.test_mnerys_gateway_native_tool_persistence import native_fixture
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs


BASE = {
    "gateway_profile": "notes_v1", "session": "session-1",
    "mode": "agent", "no_memory": "true",
    "use_rag": "false", "active_doc_state": "none",
}


def route_fixture(monkeypatch):
    from tests.test_mnerys_gateway_composed_tools import configured

    admission, session, ports, opts, ignored_writes, partials = native_fixture(monkeypatch)
    writes = []
    session.notes_route_events = []
    session.notes_route_post_jobs = []
    store = ToolApprovalStore()
    gateway = GatewayTurns(TurnService())
    # Own a private in-memory SQL schema for each synthetic Web route test.
    # Never reuse the running application's configured DB connection.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from src.gateway_notes_decision_fence import (
        NotesDecisionFence, notes_fence_metadata,
    )
    from src.gateway_notes_event_journal import (
        NotesEventJournal, notes_event_journal_metadata,
    )
    isolated_engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    notes_fence_metadata().create_all(isolated_engine)
    notes_event_journal_metadata().create_all(isolated_engine)
    notes_fence = NotesDecisionFence(isolated_engine)
    notes_journal = NotesEventJournal(isolated_engine)
    monkeypatch.setattr(
        chat_routes, "server_notes_decision_fence", lambda: notes_fence,
    )
    monkeypatch.setattr(
        chat_routes, "server_notes_event_journal", lambda: notes_journal,
    )
    fake_manager = SimpleNamespace(
        get_session=lambda sid: session if sid == session.id else (_ for _ in ()).throw(KeyError(sid)),
        save_sessions=lambda: None,
    )

    def save(current, manager, session_id, text, metrics, **kwargs):
        assert current is session and manager is fake_manager and session_id == session.id
        writes.append(text)
        session.notes_route_events.append("assistant_saved")
        current.messages.append({"role": "assistant", "content": text})
        current.history.append(ChatMessage(
            "assistant", text,
            metadata={"_db_id": f"real-router-assistant-{len(current.history) + 1}"},
        ))

    monkeypatch.setattr(notes_adapter, "LegacyChatContextPorts", lambda: ports)
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda req, sid: admission)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(chat_routes, "is_internal_tool_request", lambda request: False)
    monkeypatch.setattr(chat_routes, "_reconcile_selected_route_from_request", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "resolve_session_auth", lambda *a, **k: None)
    monkeypatch.setattr(
        chat_routes, "fire_message_event",
        lambda *a, **k: session.notes_route_events.append("user_notified"),
    )
    monkeypatch.setattr(
        chat_routes, "run_post_response_tasks",
        lambda *a, **k: (
            session.notes_route_events.append("post_response"),
            session.notes_route_post_jobs.append((a, k)),
        ),
    )
    monkeypatch.setattr(chat_routes, "save_assistant_response", save)
    monkeypatch.setattr(chat_routes, "append_interrupted_assistant_response", lambda *args: None)
    monkeypatch.setattr(chat_routes, "gateway_turns", gateway)
    monkeypatch.setattr(chat_routes, "tool_approval_store", store)
    monkeypatch.setattr("src.settings.get_setting", lambda *a, **k: [])
    monkeypatch.setenv("MNERYS_M21_WEB_NATIVE_NOTES_ROUTING", "1")

    router = chat_routes.setup_chat_routes(
        fake_manager, opts["chat_handler"], opts["chat_processor"],
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
    )
    routes = {route.path: route.endpoint for route in router.routes}
    return admission, session, ports, writes, partials, store, gateway, routes


def request(form, *, run_id=None, cursor=None):
    req = _RouteRequest("agent")
    req._form = dict(BASE) | form
    if run_id is not None:
        req.headers["X-Odysseus-Run-Id"] = run_id
    if cursor is not None:
        req.headers["Last-Event-ID"] = cursor
    return req


@pytest.mark.asyncio
async def test_actual_native_route_approves_once_with_committed_user_and_assistant(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, routes = route_fixture(monkeypatch)
    pending = []
    model_calls = []

    async def propose(_url, _name, _messages, **kwargs):
        model_calls.append("proposal")
        content = '{"action":"list"}'
        record = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="actual-http-route-test", tool_name="manage_notes",
            content=content, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", content),
            selected_tools={"manage_notes"},
            request_authority=kwargs["request_authority"],
        )
        pending.append(record)
        yield approval_requested_event(record)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": record.public_payload()})
        yield 'data: {"type":"final_response","content":"Review required"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", propose)
    import src.agent_runtime.authority as authority
    human_auth_checks = []
    monkeypatch.setattr(
        authority, "require_user_approval_request",
        lambda req: human_auth_checks.append("interactive"),
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("The legacy Web approval consumer must never run")

    monkeypatch.setattr(gateway, "decide_tool_approval", unexpected)

    try:
        first = await routes["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        first_frames = [chunk async for chunk in first.body_iterator]
        first_run = first.headers["x-odysseus-run-id"]
        assert len(first_run) == 32
        assert len(pending) == 1 and store.peek(pending[0].approval_id) is pending[0]
        assert pending[0].origin_profile == "notes_v1"
        assert '"type":"ask_user"' in "".join(first_frames)
        assert "PRIVATE" not in repr(first_frames)
        assert model_calls == ["proposal"]
        assert [item.role for item in session.history] == ["user"]
        assert ports.operations.count("save_user") == 1
        assert writes == []
        assert session.notes_route_events == ["user_notified"]
        assert session.notes_route_post_jobs == []
        run = agent_runs.get_matching_run(session.id, first_run, mode="typed")
        assert run is not None and run.web_native_notes and run.status == "done"

        # The same opaque ID must not become a legacy approval, even if
        # the originating run's short replay buffer has been evicted.
        legacy = _RouteRequest("agent")
        legacy._form = {
            "session": session.id, "message": "",
            "tool_approval_id": pending[0].approval_id,
            "tool_approval_decision": "approve_task",
        }
        original_registry_run = agent_runs._RUNS.pop(session.id)
        try:
            with pytest.raises(HTTPException) as cross_profile:
                await routes["/api/chat_stream"](legacy)
        finally:
            agent_runs._RUNS[session.id] = original_registry_run
        assert cross_profile.value.status_code == 409
        assert store.peek(pending[0].approval_id) is pending[0]
        assert model_calls == ["proposal"] and writes == []

        # A browser button must enter the new composer directly, not the
        # old route's consume/mark/save path.
        actions = []
        async def continue_model(_url, _name, _messages, **kwargs):
            model_calls.append("continuation")
            exact = kwargs["exact_approval"]
            assert exact.pending is pending[0]
            assert exact.claim(
                owner="alice", session_id=session.id,
                tool_name="manage_notes", content=pending[0].content,
                workspace=None,
            )
            actions.append("once")
            yield tool_event_frame({
                "type": "tool_start", "tool": "manage_notes", "round": 1,
            })
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes",
                "round": 1, "exit_code": 0, "output": "PRIVATE",
            })
            yield 'data: {"delta":"You have some notes."}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(runtime, "stream_agent_loop", continue_model)
        second = await routes["/api/chat_stream"](
            request({
                "tool_approval_id": pending[0].approval_id,
                "tool_approval_decision": "approve_task",
            }),
        )
        second_frames = [chunk async for chunk in second.body_iterator]
        assert human_auth_checks == ["interactive"]
        assert second.headers["x-odysseus-run-id"] != first_run
        assert actions == ["once"]
        assert model_calls == ["proposal", "continuation"]
        assert store.peek(pending[0].approval_id) is None
        assert [item.role for item in session.history] == ["user", "assistant"]
        assert ports.operations.count("save_user") == 1
        assert writes == ["You have some notes."]
        assert session.notes_route_events == [
            "user_notified", "assistant_saved", "post_response",
        ]
        assert len(session.notes_route_post_jobs) == 1
        post_args, post_kw = session.notes_route_post_jobs[0]
        assert post_args[0] is session
        assert post_args[2] == session.id
        assert post_args[3:6] == ("List my notes", "You have some notes.", None)
        assert post_kw["owner"] == "alice"
        assert post_kw["allow_background_extraction"] is False
        assert post_kw["extract_skills"] is False
        assert not partials
        assert '"type":"message_saved"' in "".join(second_frames)
        assert "PRIVATE" not in repr(second_frames)
        terminal_frames = [chunk for chunk in second_frames if "[DONE]" in chunk]
        assert len(terminal_frames) == 1
        assert terminal_frames[0].startswith(
            f"id: {second.headers['x-odysseus-run-id']}:6\n"
        )
        assert terminal_frames[0].endswith("data: [DONE]\n\n")

        with pytest.raises(HTTPException) as replay:
            await routes["/api/chat_stream"](
                request({
                    "tool_approval_id": pending[0].approval_id,
                    "tool_approval_decision": "approve_task",
                }),
            )
        assert replay.value.status_code in {403, 409}
        assert actions == ["once"]
        assert len(session.notes_route_post_jobs) == 1
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_real_asgi_transport_form_request_and_completed_resume(monkeypatch):
    """HTTP form parsing and SSE replay use the real mounted route callback."""
    import httpx
    from fastapi import FastAPI

    admission, session, ports, writes, partials, store, gateway, routes = route_fixture(monkeypatch)
    monkeypatch.setattr(chat_routes, "require_api_token_scope", lambda *a, **k: None)
    model_calls = []

    async def model(_url, _name, _messages, **kwargs):
        model_calls.append("one")
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE_NOTES",
        })
        yield 'data: {"delta":"Notes from Web."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    app = FastAPI()
    app.post("/api/chat_stream")(routes["/api/chat_stream"])
    app.get("/api/chat/resume/{session_id}")(routes["/api/chat/resume/{session_id}"])
    app.post("/api/chat/stop/{session_id}")(routes["/api/chat/stop/{session_id}"])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.com",
        ) as client:
            sent = await client.post(
                "/api/chat_stream", data=dict(BASE) | {"message": "List my notes"},
            )
            assert sent.status_code == 200
            run_id = sent.headers["x-odysseus-run-id"]
            assert f"id: {run_id}:0\n" in sent.text
            assert '"type":"message_saved"' in sent.text
            assert sent.text.count("data: [DONE]") == 1
            assert "PRIVATE_NOTES" not in sent.text
            assert model_calls == ["one"] and writes == ["Notes from Web."]
            assert ports.operations.count("save_user") == 1
            assert len(session.notes_route_post_jobs) == 1
            assert session.notes_route_events == [
                "user_notified", "assistant_saved", "post_response",
            ]

            replay = await client.get(
                f"/api/chat/resume/{session.id}",
                headers={
                    "X-Odysseus-Run-Id": run_id,
                    "Last-Event-ID": f"{run_id}:0",
                },
            )
            assert replay.status_code == 200
            assert replay.headers["x-odysseus-run-id"] == run_id
            assert '"type":"tool_start"' not in replay.text
            assert '"type":"message_saved"' in replay.text
            assert replay.text.count("data: [DONE]") == 1
            assert model_calls == ["one"] and writes == ["Notes from Web."]
            assert len(session.notes_route_post_jobs) == 1
            stopped = await client.post(
                f"/api/chat/stop/{session.id}",
                headers={"X-Odysseus-Run-Id": run_id},
            )
            assert stopped.status_code == 200
            assert stopped.json() == {"stopped": False}
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_global_notes_disable_blocks_before_model_or_user_append(monkeypatch):
    from src.gateway_admission import AdmissionDenied

    admission, session, ports, writes, partials, store, gateway, routes = route_fixture(monkeypatch)
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: ["manage_notes"] if key == "disabled_tools" else default,
    )
    async def prohibited(*args, **kwargs):
        raise AssertionError("Global disabled tool was invoked")
        yield

    monkeypatch.setattr(runtime, "stream_agent_loop", prohibited)
    with pytest.raises(HTTPException) as denied:
        await routes["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
    assert denied.value.status_code == 403
    assert not ports.operations and not writes and not partials
    assert session.history == [] and session.messages == []
    assert session.notes_route_post_jobs == []
    assert agent_runs.get_active_run(session.id) is None


@pytest.mark.asyncio
async def test_real_resume_and_stop_route_preserve_exact_typed_run(monkeypatch):
    admission, session, ports, writes, partials, store, gateway, routes = route_fixture(monkeypatch)
    proceed = asyncio.Event()
    gate = asyncio.Event()
    call_count = []

    async def model(*args, **kwargs):
        call_count.append(True)
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        gate.set()
        await proceed.wait()
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"One response."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    send = None
    try:
        sent = await routes["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        run_id = sent.headers["x-odysseus-run-id"]
        subscriber = sent.body_iterator
        first = await asyncio.wait_for(anext(subscriber), 4)
        assert f"id: {run_id}:0" in first
        await subscriber.aclose()

        stop = routes["/api/chat/stop/{session_id}"]
        assert await stop(request({}, run_id="f" * 32), session.id) == {"stopped": False}
        assert agent_runs.get_active_run(session.id).run_id == run_id
        proceed.set()
        current = agent_runs.get_matching_run(session.id, run_id, mode="typed")
        await asyncio.wait_for(current.task, 4)
        assert current.status == "done"
        assert len(writes) == 1 and call_count == [True]

        resume = routes["/api/chat/resume/{session_id}"]
        resumed = await resume(
            request({}, run_id=run_id, cursor=f"{run_id}:0"),
            session.id,
        )
        output = [chunk async for chunk in resumed.body_iterator]
        assert resumed.headers["x-odysseus-run-id"] == run_id
        assert not any('"type":"tool_start"' in chunk for chunk in output)
        assert any('"type":"message_saved"' in chunk for chunk in output)
        assert output[-1].endswith("data: [DONE]\n\n")
        assert len(writes) == 1 and call_count == [True]
        assert await stop(request({}, run_id=run_id), session.id) == {"stopped": False}
    finally:
        proceed.set()
        await clear_runs(session.id)
