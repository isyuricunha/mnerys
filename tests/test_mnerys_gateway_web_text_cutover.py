"""Controlled real HTTP strict-text cutover and fail-closed legacy selection.

No external provider, production session, real DB, Web UI or deployment.
Web and Gateway use the same inherited Agent Loop/session writers here.
"""

from dataclasses import replace
import asyncio

import httpx
from fastapi import FastAPI
import pytest

from routes import gateway_text_adapter
from src import agent_runs
from src import turn_service as runtime
from src.gateway_turns import GatewayTurns
from src.gateway_web_text_cutover import eligible_strict_web_text_turn
from src.turn_service import TurnService
from core.models import ChatMessage
from tests.test_mnerys_gateway_text_persistence import PersistPorts, persisted_fixture


class _WebChatPorts(PersistPorts):
    """Mirror the legacy writer while asserting the Web chat marker."""

    def append_user_message(
        self, session, _handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        assert incognito is False
        assert interaction_mode == "chat"
        assert auto_escalated is False
        self.operations.append("save_user")
        session.messages.append({
            "role": "user", "content": preprocessed.user_content,
        })
        session.history.append(ChatMessage(
            "user", preprocessed.user_content,
            metadata={"_db_id": f"fixture-user-{len(session.history) + 1}"},
        ))


def _chat_ports(options):
    ports = _WebChatPorts()
    options["ports"] = ports
    return ports


def eligible(**changes):
    values = {
        "message": "A simple question",
        "session": "session-1",
        "gateway_profile": "text_v1",
        "no_memory": "true",
        "use_rag": "false",
        "active_doc_state": "none",
    }
    params = dict(
        server_enabled=True,
        form_values=values,
        json_values=None,
        message=values["message"],
        chat_mode="chat",
        no_memory=True,
        use_rag="false",
        use_web=None,
        allow_web_search=None,
        thinking_mode="off",
        reasoning_effort=None,
        temperature_override=None,
        selected_endpoint_id="",
        approved_plan="",
        preset_id=None,
        active_doc_id="",
        active_doc_state="none",
        active_email_uid="",
        selected_note_ids=[],
        attachments=[],
        workspace=None,
        workspace_rejected=None,
        client_runtime_context=None,
        incognito=False,
        compare_mode=False,
        plan_mode=False,
        tool_approval_continuation=False,
        do_research=False,
        image_generation_session=False,
        foreground_fallback_enabled=False,
        external_untrusted_context_seen=False,
        internal_tool_request=False,
        agent_permitted=True,
    )
    return eligible_strict_web_text_turn(**(params | changes))


def test_selected_explicit_no_tools_profile_only():
    assert eligible()
    assert not eligible(server_enabled=False)
    assert not eligible(form_values={"message": "Hi", "session": "session-1"})
    assert not eligible(form_values={
        "message": "Hi", "session": "session-1",
        "gateway_profile": "text_v1", "no_memory": "true",
        "use_rag": "false", "allow_bash": "true",
    })
    assert not eligible(form_values={
        "message": "Hi", "session": "session-1",
        "gateway_profile": "agent_v1", "no_memory": "true",
        "use_rag": "false",
    })


@pytest.mark.parametrize("difference", [
    {"message": ""},
    {"message": "x" * 16385},
    {"chat_mode": "agent"},
    {"no_memory": False},
    {"use_rag": None},
    {"use_rag": "true"},
    {"use_web": "true"},
    {"allow_web_search": "true"},
    {"thinking_mode": "on"},
    {"reasoning_effort": "high"},
    {"temperature_override": 0.5},
    {"selected_endpoint_id": "another-endpoint"},
    {"approved_plan": "another plan"},
    {"preset_id": "other"},
    {"active_doc_id": "doc"},
    {"active_email_uid": "private"},
    {"selected_note_ids": ["note"]},
    {"attachments": ["file"]},
    {"workspace": "/tmp/private"},
    {"workspace_rejected": "/tmp/forbidden"},
    {"client_runtime_context": {"surface": "tui"}},
    {"incognito": True},
    {"compare_mode": True},
    {"plan_mode": True},
    {"tool_approval_continuation": True},
    {"do_research": True},
    {"image_generation_session": True},
    {"foreground_fallback_enabled": True},
    {"external_untrusted_context_seen": True},
    {"internal_tool_request": True},
    {"agent_permitted": False},
    {"form_values": {"gateway_profile": "text_v1", "unknown": "unexpected"}},
    {"json_values": {"gateway_profile": "text_v1", "use_web": "true"}},
])
def test_unsupported_web_features_keep_inherited_web_path(difference):
    assert not eligible(**difference)


@pytest.mark.asyncio
async def test_real_http_adapter_uses_one_existing_loop_and_persisted_writer(
    monkeypatch,
):
    admission, session, _original_ports, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(
        gateway_text_adapter, "LegacyChatContextPorts",
        lambda: ports,
    )
    calls = []

    async def model(endpoint, name, messages, **kw):
        calls.append((endpoint, name))
        yield 'data: {"delta":"Answered through Core."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    p = opts["persistence"]
    app = FastAPI()

    @app.post("/api/chat_stream")
    async def web_turn():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway,
            admission=admission,
            session=session,
            text="A simple question",
            session_manager=p.session_manager,
            chat_handler=opts["chat_handler"],
            chat_processor=opts["chat_processor"],
            privileges={},
            assistant_writer=p.saver,
            interrupted_writer=p.interrupted_writer,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test.invalid",
    ) as client:
        response = await client.post("/api/chat_stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    run_id = response.headers["x-odysseus-run-id"]
    assert len(run_id) == 32

    data = response.text
    assert '"type": "turn_mode"' in data
    assert '"type": "model_info"' in data
    assert '"delta":"Answered through Core."' in data
    assert '"type":"message_saved","id":"fixture-assistant-2"' in data
    assert '"type":"final_response","content":"Answered through Core."' in data
    assert "data: [DONE]\n\n" in data
    assert data.count("data: [DONE]") == 1
    assert [item.role for item in session.history] == ["user", "assistant"]
    assert len(saved) == 1
    assert calls == [(session.endpoint_url, session.model)]
    assert ports.operations.count("save_user") == 1

    run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
    assert run is not None
    assert run.status == "done"
    await run.task
    agent_runs._RUNS.pop(session.id, None)
    if run.evict_task:
        run.evict_task.cancel()
        await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_adapter_sanitizes_writer_failure(
    monkeypatch,
):
    admission, session, _original_ports, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(
        gateway_text_adapter, "LegacyChatContextPorts", lambda: ports,
    )

    def failing_saver(*args, **kwargs):
        raise RuntimeError("private credential in a writer exception")

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Visible draft"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    p = opts["persistence"]
    gateway = GatewayTurns(TurnService())
    app = FastAPI()

    @app.post("/api/chat_stream")
    async def web_turn():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway,
            admission=admission,
            session=session,
            text="A simple question",
            session_manager=p.session_manager,
            chat_handler=opts["chat_handler"],
            chat_processor=opts["chat_processor"],
            privileges={},
            assistant_writer=failing_saver,
            interrupted_writer=None,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test.invalid",
    ) as client:
        response = await client.post("/api/chat_stream")

    assert response.status_code == 200
    assert '"delta":"Visible draft"' in response.text
    assert "event: error\n" in response.text
    assert "private credential" not in response.text
    assert "message_saved" not in response.text
    assert "final_response" not in response.text
    assert "[DONE]" not in response.text
    assert not saved
    run_id = response.headers["x-odysseus-run-id"]
    run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
    assert run is not None and run.status == "error"
    await run.task
    agent_runs._RUNS.pop(session.id, None)
    if run.evict_task:
        run.evict_task.cancel()
        await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_web_chat_route_dispatches_opted_in_strict_profile(
    monkeypatch,
):
    import routes.chat_routes as chat_routes
    from tests.test_foreground_model_routing import (
        _chat_stream_endpoint, _RouteRequest,
    )

    captures = {}
    route = _chat_stream_endpoint(
        monkeypatch, "chat", captures, capture_context=True,
    )
    monkeypatch.setenv("MNERYS_M21_WEB_TEXT_ROUTING", "1")
    request = _RouteRequest("chat")
    request._form = {
        "message": "hello",
        "session": "session-1",
        "mode": "chat",
        "gateway_profile": "text_v1",
        "no_memory": "true",
        "use_rag": "false",
        "active_doc_state": "none",
    }
    returned = object()
    calls = []

    def routed(**kwargs):
        calls.append(kwargs)
        return returned

    monkeypatch.setattr(
        chat_routes, "start_strict_text_web_response", routed,
    )
    response = await route(request)
    assert response is returned
    assert len(calls) == 1
    assert calls[0]["text"] == "hello"
    assert calls[0]["admission"].session_id == "session-1"
    assert calls[0]["session"].id == "session-1"
    assert callable(calls[0]["assistant_writer"])
    assert calls[0]["post_response_runner"] is chat_routes.run_post_response_tasks
    assert callable(calls[0]["after_user_saved"])
    assert "memory_manager" in calls[0]
    assert "memory_vector" in calls[0]
    assert "webhook_manager" in calls[0]
    assert "chat" not in captures
    assert "agent" not in captures
    assert "build_context" not in captures


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"disable_switch": True},
    {"field": ("allow_bash", "true")},
    {"field": ("use_web", "true")},
    {"field": ("no_memory", "false")},
    {"field": ("active_doc_id", "forged")},
])
async def test_actual_web_chat_route_preserves_legacy_for_unsupported_profile(
    monkeypatch, change,
):
    import routes.chat_routes as chat_routes
    from tests.test_foreground_model_routing import (
        _chat_stream_endpoint, _RouteRequest,
    )

    captures = {}
    route = _chat_stream_endpoint(
        monkeypatch, "chat", captures, capture_context=True,
    )
    if change.get("disable_switch"):
        monkeypatch.delenv("MNERYS_M21_WEB_TEXT_ROUTING", raising=False)
    else:
        monkeypatch.setenv("MNERYS_M21_WEB_TEXT_ROUTING", "1")
    request = _RouteRequest("chat")
    request._form = {
        "message": "hello",
        "session": "session-1",
        "mode": "chat",
        "gateway_profile": "text_v1",
        "no_memory": "true",
        "use_rag": "false",
        "active_doc_state": "none",
    }
    if "field" in change:
        key, value = change["field"]
        request._form[key] = value

    def unexpected(**kwargs):
        pytest.fail("Unsupported Web feature entered strict Gateway cutover")

    monkeypatch.setattr(
        chat_routes, "start_strict_text_web_response", unexpected,
    )
    response = await route(request)
    assert response.status_code == 200
    async for _ in response.body_iterator:
        pass
    assert "chat" in captures or "agent" in captures
    assert "build_context" in captures



@pytest.mark.asyncio
async def test_http_resume_replays_exact_typed_run_without_reinvoking_model(monkeypatch):
    from fastapi import HTTPException
    admission, session, _unused, options, saved = persisted_fixture()
    ports = _chat_ports(options)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    ready = asyncio.Event()
    release = asyncio.Event()
    model_calls = []

    async def model(*args, **kwargs):
        model_calls.append(1)
        yield 'data: {"delta":"Reconnected answer."}\n\n'
        ready.set()
        await release.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    manager = options["persistence"].session_manager
    app = FastAPI()

    @app.post("/api/chat_stream")
    async def send():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            text="Plain text", session_manager=manager,
            chat_handler=options["chat_handler"],
            chat_processor=options["chat_processor"],
            privileges={}, assistant_writer=options["persistence"].saver,
            interrupted_writer=None,
        )

    @app.get("/api/chat/resume/{session_id}")
    async def resume(session_id: str, run_id: str, cursor: str):
        if session_id != session.id:
            raise HTTPException(404)
        reply = gateway_text_adapter.resume_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            expected_run_id=run_id, after_cursor=cursor,
        )
        if reply is None:
            raise HTTPException(404)
        return reply

    run = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test.invalid",
        ) as client:
            original = asyncio.create_task(client.post("/api/chat_stream"))
            await asyncio.wait_for(ready.wait(), timeout=5)
            run = agent_runs.get_active_run(session.id)
            assert run is not None and run.mode == "typed"
            assert run.buffer and run.buffer[0].type == "message.delta"
            release.set()
            first_response = await asyncio.wait_for(original, timeout=5)
            await asyncio.wait_for(run.task, timeout=5)
            assert first_response.status_code == 200
            assert first_response.text.count("data: [DONE]") == 1
            assert len(saved) == 1

            replayed = await client.get(
                f"/api/chat/resume/{session.id}",
                params={"run_id": run.run_id, "cursor": f"{run.run_id}:0"},
            )
            assert replayed.status_code == 200
            assert '"type":"message_saved"' in replayed.text
            assert '"type":"final_response"' in replayed.text
            assert replayed.text.count("data: [DONE]") == 1
            assert len(model_calls) == 1
            assert len(saved) == 1
            stale = await client.get(
                f"/api/chat/resume/{session.id}",
                params={"run_id": "0" * 32, "cursor": f"{run.run_id}:0"},
            )
            assert stale.status_code == 404
    finally:
        release.set()
        if run is not None:
            agent_runs._RUNS.pop(session.id, None)
            if run.task and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            if run.evict_task:
                run.evict_task.cancel()
                await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_stop_blocks_stale_id_and_cancels_only_current_typed_run(
    monkeypatch,
):
    admission, session, _unused, options, saved = persisted_fixture()
    ports = _chat_ports(options)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    started = asyncio.Event()
    gate = asyncio.Event()

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Partial only."}\n\n'
        started.set()
        await gate.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    app = FastAPI()

    @app.post("/api/chat_stream")
    async def send():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            text="Plain text",
            session_manager=options["persistence"].session_manager,
            chat_handler=options["chat_handler"],
            chat_processor=options["chat_processor"],
            privileges={}, assistant_writer=options["persistence"].saver,
            interrupted_writer=None,
        )

    @app.post("/api/chat/stop/{session_id}")
    async def stop(session_id: str, run_id: str):
        if session_id != session.id:
            return {"stopped": False}
        return {"stopped": gateway.stop(admission, run_id)}

    run = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test.invalid",
        ) as client:
            request = asyncio.create_task(client.post("/api/chat_stream"))
            await asyncio.wait_for(started.wait(), timeout=5)
            run = agent_runs.get_active_run(session.id)
            assert run is not None and run.status == "running"
            rejected = await client.post(
                f"/api/chat/stop/{session.id}", params={"run_id": "0" * 32},
            )
            assert rejected.json() == {"stopped": False}
            assert run.status == "running"
            stopped = await client.post(
                f"/api/chat/stop/{session.id}", params={"run_id": run.run_id},
            )
            assert stopped.json() == {"stopped": True}
            await asyncio.wait_for(run.task, timeout=5)
            response = await asyncio.wait_for(request, timeout=5)
            assert response.status_code == 200
            assert run.status == "stopped"
            assert run.outcome.state == "cancelled"
            assert not saved
            assert '"type":"message_saved"' not in response.text
            assert "data: [DONE]" not in response.text
    finally:
        gate.set()
        if run is not None:
            agent_runs._RUNS.pop(session.id, None)
            if run.task and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            if run.evict_task:
                run.evict_task.cancel()
                await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_resume_rejects_in_process_typed_runs_from_another_adapter():
    from src.gateway_turn_events import TurnEvent, TurnOutcome

    admission, session, ports, opts, saved = persisted_fixture()
    outcome = TurnOutcome()

    async def internal_turn():
        outcome.state = "running"
        outcome.last_sequence = 1
        yield TurnEvent(1, "message.delta", {"text": "Private internal work"})
        outcome.state = "completed"
        outcome.last_sequence = 2
        yield TurnEvent(2, "turn.completed", {})

    turns = TurnService()
    stream = turns.start_detached_typed(session.id, internal_turn(), outcome)
    try:
        assert gateway_text_adapter.resume_strict_text_web_response(
            gateway=GatewayTurns(turns),
            admission=admission,
            session=session,
            expected_run_id=stream.run_id,
            after_cursor=None,
        ) is None
        await asyncio.gather(
            agent_runs.get_matching_run(
                session.id, stream.run_id, mode="typed",
            ).task,
        )
        assert not agent_runs.get_matching_run(
            session.id, stream.run_id, mode="typed",
        ).web_strict_text
    finally:
        run = agent_runs._RUNS.pop(session.id, None)
        if run and run.evict_task:
            run.evict_task.cancel()
            await asyncio.gather(run.evict_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exact_web_resume_keeps_same_sse_cursor_and_saved_receipt(
    monkeypatch,
):
    admission, session, _original_ports, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(
        gateway_text_adapter, "LegacyChatContextPorts", lambda: ports,
    )
    async def model(*args, **kwargs):
        yield 'data: {"delta":"Saved through Web."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    p = opts["persistence"]
    turns = TurnService()
    gateway = GatewayTurns(turns)
    first_response = gateway_text_adapter.start_strict_text_web_response(
        gateway=gateway, admission=admission, session=session,
        text="A simple question", session_manager=p.session_manager,
        chat_handler=opts["chat_handler"],
        chat_processor=opts["chat_processor"], privileges={},
        assistant_writer=p.saver, interrupted_writer=p.interrupted_writer,
    )
    first = [chunk async for chunk in first_response.body_iterator]
    run_id = first_response.headers["x-odysseus-run-id"]
    run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
    assert run is not None and run.web_strict_text
    await run.task
    try:
        saved_index = next(
            i for i, event in enumerate(run.buffer)
            if event.type == "message.saved"
        )
        cursor = f"{run_id}:{saved_index}"
        continuation = gateway_text_adapter.resume_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            expected_run_id=run_id, after_cursor=cursor,
        )
        assert continuation is not None
        resumed = [chunk async for chunk in continuation.body_iterator]
        assert len(resumed) == 2
        assert resumed[0].startswith(f"id: {run_id}:{saved_index + 1}\n")
        assert '"type":"final_response"' in resumed[0]
        assert resumed[-1].startswith(f"id: {run_id}:{saved_index + 2}\n")
        assert "[DONE]" in resumed[-1]
        assert len(saved) == 1
    finally:
        agent_runs._RUNS.pop(session.id, None)
        if run.evict_task:
            run.evict_task.cancel()
            await asyncio.gather(run.evict_task, return_exceptions=True)
