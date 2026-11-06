"""Post-response parity for the actual opted-in notes_v1 Web adapter.

All jobs are intercepted. Neither optional extraction nor an external API
is invoked; only the inherited Gateway and detached run lifecycle execute.
"""

import pytest

import routes.chat_routes as routes
from src import agent_runs, turn_service as runtime
from src.agent_tool_events import tool_event_frame
from tests.test_mnerys_gateway_detached_typed_lifecycle import clear_runs
from tests.test_mnerys_gateway_web_native_notes_integration import (
    request, route_fixture,
)


async def notes_model(*_args, **_kwargs):
    yield tool_event_frame({
        "type": "tool_start", "tool": "manage_notes", "round": 1,
    })
    yield tool_event_frame({
        "type": "tool_output", "tool": "manage_notes",
        "round": 1, "exit_code": 0, "output": "PRIVATE_NOTE_TEXT",
    })
    yield 'data: {"delta":"Notes complete."}\n\n'
    yield "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_post_response_follows_committed_writer_and_agent_cleanup_once(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    order = []
    original_writer = routes.save_assistant_response

    def writer(*args, **kwargs):
        result = original_writer(*args, **kwargs)
        order.append("assistant_committed")
        return result

    async def model(*args, **kwargs):
        order.append("model_started")
        try:
            async for chunk in notes_model(*args, **kwargs):
                yield chunk
        finally:
            order.append("model_closed")

    jobs = []

    def runner(*args, **kwargs):
        assert order == [
            "model_started", "assistant_committed", "model_closed",
        ]
        assert len(writes) == 1
        assert session.history[-1].role == "assistant"
        jobs.append((args, kwargs))
        order.append("post_response")

    monkeypatch.setattr(routes, "save_assistant_response", writer)
    monkeypatch.setattr(routes, "run_post_response_tasks", runner)
    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        response = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        chunks = [chunk async for chunk in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        assert order == [
            "model_started", "assistant_committed", "model_closed",
            "post_response",
        ]
        assert len(jobs) == 1
        args, kwargs = jobs[0]
        assert args[0] is session
        assert args[2:6] == (
            session.id, "List my notes", "Notes complete.", None,
        )
        assert args[6] == ports.load_preferences("alice")
        assert kwargs["owner"] == "alice"
        assert kwargs["allow_background_extraction"] is False
        assert kwargs["extract_skills"] is False
        assert session.notes_route_events[0] == "user_notified"
        assert sum("message_saved" in chunk for chunk in chunks) == 1
        assert sum("[DONE]" in chunk for chunk in chunks) == 1
        resumed = await endpoints["/api/chat/resume/{session_id}"](
            request({}, run_id=run_id, cursor=f"{run_id}:0"), session.id,
        )
        _ = [chunk async for chunk in resumed.body_iterator]
        assert len(jobs) == len(writes) == 1
        assert ports.operations.count("save_user") == 1
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_writer_failure_cannot_schedule_jobs_or_claim_saved_answer(
    monkeypatch,
):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    attempts = []

    def failed_writer(*args, **kwargs):
        attempts.append("writer_attempt")
        raise RuntimeError("synthetic private writer failure token")

    monkeypatch.setattr(routes, "save_assistant_response", failed_writer)
    monkeypatch.setattr(runtime, "stream_agent_loop", notes_model)
    try:
        response = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        chunks = [chunk async for chunk in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        assert sum("event: error" in chunk for chunk in chunks) == 1
        assert not any('"type":"message_saved"' in chunk for chunk in chunks)
        assert not any('"type":"final_response"' in chunk for chunk in chunks)
        assert not any("[DONE]" in chunk for chunk in chunks)
        assert "synthetic private writer failure token" not in repr(chunks)
        assert session.notes_route_events == ["user_notified"]
        assert session.notes_route_post_jobs == []
        assert attempts == ["writer_attempt"]
        replay = await endpoints["/api/chat/resume/{session_id}"](
            request({}, run_id=run_id), session.id,
        )
        replay_chunks = [chunk async for chunk in replay.body_iterator]
        assert sum("event: error" in chunk for chunk in replay_chunks) == 1
        assert attempts == ["writer_attempt"]
        assert session.notes_route_post_jobs == []
        assert len(session.history) == 1 and session.history[0].role == "user"
        assert not writes and not partials
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_optional_job_failure_after_commit_does_not_duplicate_effect(
    monkeypatch, caplog,
):
    admission, session, ports, writes, partials, store, gateway, endpoints = (
        route_fixture(monkeypatch)
    )
    jobs = []

    def failed_post(*args, **kwargs):
        jobs.append("attempt")
        raise RuntimeError("synthetic private webhook secret")

    monkeypatch.setattr(routes, "run_post_response_tasks", failed_post)
    monkeypatch.setattr(runtime, "stream_agent_loop", notes_model)
    try:
        response = await endpoints["/api/chat_stream"](
            request({"message": "List my notes"}),
        )
        chunks = [chunk async for chunk in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        assert len(writes) == len(jobs) == 1
        assert sum('"type":"message_saved"' in chunk for chunk in chunks) == 1
        assert sum("[DONE]" in chunk for chunk in chunks) == 1
        assert not any("event: error" in chunk for chunk in chunks)
        assert "synthetic private webhook secret" not in repr(chunks)
        assert "synthetic private webhook secret" not in caplog.text
        replay = await endpoints["/api/chat/resume/{session_id}"](
            request({}, run_id=run_id, cursor=f"{run_id}:0"), session.id,
        )
        frames = [chunk async for chunk in replay.body_iterator]
        assert frames and len(jobs) == len(writes) == 1
    finally:
        await clear_runs(session.id)
