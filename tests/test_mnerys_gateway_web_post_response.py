"""Acceptance: real Web strict text cutover reuses admitted post-response jobs.

Only isolated synthetic sessions, context ports and model streams. The
original task scheduler is injected as a trusted callable and is never
executed against live user data, a real provider or production database.
"""

import asyncio

import pytest

from routes import gateway_text_adapter
from src import agent_runs, turn_service as runtime
from src.gateway_admission import AdmissionDenied
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_web_text_cutover import _chat_ports
from tests.test_mnerys_gateway_text_persistence import persisted_fixture


async def cleanup_run(session_id: str):
    run = agent_runs._RUNS.pop(session_id, None)
    if run is None:
        return
    if run.task is not None and not run.task.done():
        run.task.cancel()
    if run.task is not None:
        await asyncio.gather(run.task, return_exceptions=True)
    if run.evict_task is not None:
        run.evict_task.cancel()
        await asyncio.gather(run.evict_task, return_exceptions=True)


def web_response(gateway, admission, session, opts, **extra):
    persistence = opts["persistence"]
    return gateway_text_adapter.start_strict_text_web_response(
        gateway=gateway,
        admission=admission,
        session=session,
        text="A simple text question",
        session_manager=persistence.session_manager,
        chat_handler=opts["chat_handler"],
        chat_processor=opts["chat_processor"],
        privileges={},
        assistant_writer=extra.pop("assistant_writer", persistence.saver),
        interrupted_writer=persistence.interrupted_writer,
        **extra,
    )


@pytest.mark.asyncio
async def test_after_save_jobs_and_user_event_run_once_inside_original_detached_turn(
    monkeypatch,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    order, jobs = [], []
    manager = object()
    memory_vector = object()
    webhook_manager = object()
    existing_writer = opts["persistence"].saver
    model_calls = []

    def save_and_record(*args, **kwargs):
        result = existing_writer(*args, **kwargs)
        order.append("assistant_committed")
        return result

    async def model(*args, **kwargs):
        model_calls.append(True)
        order.append("model")
        try:
            yield 'data: {"delta":"Saved and finalized."}\n\n'
            yield 'data: {"type":"usage","input_tokens":17,"output_tokens":5}\n\n'
            yield "data: [DONE]\n\n"
        finally:
            order.append("model_closed")

    def after_user():
        order.append("user_committed")

    def post_task(*args, **kwargs):
        assert saved and len(saved) == 1
        assert session.history[-1].role == "assistant"
        assert kwargs["owner"] == "alice"
        assert kwargs["allow_background_extraction"] is False
        assert kwargs["incognito"] is False
        assert kwargs["compare_mode"] is False
        assert kwargs["extract_skills"] is False
        assert args[0] is session
        assert args[1] is opts["persistence"].session_manager
        assert args[2] == session.id
        assert args[3] == "A simple text question"
        assert args[4] == "Saved and finalized."
        assert args[6] == ports.load_preferences("alice")
        assert args[7] is manager
        assert args[8] is memory_vector
        assert args[9] is webhook_manager
        jobs.append((args, kwargs))
        order.append("post_response")

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())

    try:
        response = web_response(
            gateway, admission, session, opts,
            assistant_writer=save_and_record,
            post_response_runner=post_task,
            memory_manager=manager,
            memory_vector=memory_vector,
            webhook_manager=webhook_manager,
            after_user_saved=after_user,
        )
        frames = [frame async for frame in response.body_iterator]
        run_id = response.headers["x-odysseus-run-id"]
        run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
        await run.task

        assert order == [
            "user_committed", "model", "assistant_committed",
            "model_closed", "post_response",
        ]
        assert len(model_calls) == 1
        assert len(jobs) == 1
        assert len(saved) == 1
        assert sum('"type":"message_saved"' in frame for frame in frames) == 1
        assert sum("data: [DONE]" in frame for frame in frames) == 1

        # An exact run replay reads the existing buffer, not the writer or
        # post-response dispatcher again.
        resumed = gateway_text_adapter.resume_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            expected_run_id=run_id, after_cursor=f"{run_id}:0",
        )
        assert resumed is not None
        replay = [frame async for frame in resumed.body_iterator]
        assert replay and any("message_saved" in f for f in replay)
        assert len(model_calls) == len(jobs) == len(saved) == 1
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_writer_failure_never_dispatches_completed_jobs_or_message_receipt(
    monkeypatch,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    calls = []

    def writer(*args, **kwargs):
        calls.append("write_attempted")
        raise RuntimeError("private synthetic writer credential")

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Visible draft"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        response = web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            assistant_writer=writer,
            post_response_runner=lambda *a, **k: calls.append("job"),
            after_user_saved=lambda: calls.append("user"),
        )
        frames = [f async for f in response.body_iterator]
        assert calls == ["user", "write_attempted"]
        assert sum("event: error" in f for f in frames) == 1
        assert not any("message_saved" in f or "[DONE]" in f for f in frames)
        assert not saved
        assert "private synthetic" not in repr(frames)
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_post_response_failure_after_confirmed_write_cannot_repeat_or_undo_save(
    monkeypatch, caplog,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    tasks = []

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Already committed"}\n\n'
        yield "data: [DONE]\n\n"

    def failing_dispatch(*args, **kwargs):
        tasks.append(True)
        raise RuntimeError("private token from failed optional post task")

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        response = web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            post_response_runner=failing_dispatch,
        )
        frames = [f async for f in response.body_iterator]
        assert len(saved) == len(tasks) == 1
        assert sum("message_saved" in f for f in frames) == 1
        assert sum("[DONE]" in f for f in frames) == 1
        assert not any("event: error" in f for f in frames)
        assert "private token" not in caplog.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_uncommitted_user_input_does_not_fire_web_message_event_or_call_model(
    monkeypatch,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    counters = []

    def malformed_user(session, handler, preprocessed, **kwargs):
        counters.append("user_attempt")
        # A legacy saver that silently returns without a new DB receipt.
        return None

    ports.append_user_message = malformed_user

    async def unexpected_model(*args, **kwargs):
        counters.append("model")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", unexpected_model)
    try:
        response = web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            after_user_saved=lambda: counters.append("user_event"),
            post_response_runner=lambda *args, **kwargs: counters.append("post"),
        )
        frames = [frame async for frame in response.body_iterator]
        assert counters == ["user_attempt"]
        assert not saved
        assert not any("message_saved" in f or "[DONE]" in f for f in frames)
        assert any("event: error" in f for f in frames)
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_cancelled_partial_never_fires_completed_post_response_job(
    monkeypatch,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    started = asyncio.Event()
    gate = asyncio.Event()
    calls = []

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Partial only"}\n\n'
        started.set()
        await gate.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    gateway = GatewayTurns(TurnService())
    run = None
    try:
        response = web_response(
            gateway, admission, session, opts,
            post_response_runner=lambda *args, **kwargs: calls.append("post"),
            after_user_saved=lambda: calls.append("user"),
        )
        async def consume():
            return [frame async for frame in response.body_iterator]

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        run_id = response.headers["x-odysseus-run-id"]
        run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
        assert run is not None
        assert gateway.stop(admission, run_id) is True
        await asyncio.wait_for(run.task, timeout=5)
        frames = await asyncio.wait_for(consumer, timeout=5)
        assert calls == ["user"]
        assert run.outcome.state == "cancelled"
        assert not saved
        assert not any("message_saved" in f or "[DONE]" in f for f in frames)
    finally:
        gate.set()
        await cleanup_run(session.id)


def test_invalid_post_response_callback_is_rejected_before_run_creation(monkeypatch):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    with pytest.raises(AdmissionDenied):
        web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            post_response_runner="untrusted-script",
        )
    assert not saved
    assert not ports.operations
    assert agent_runs.get_active_run(session.id) is None



@pytest.mark.asyncio
async def test_user_message_event_failure_after_receipt_cannot_duplicate_user_write(
    monkeypatch, caplog,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    calls = []

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Completed after notification error"}\n\n'
        yield "data: [DONE]\n\n"

    def broken_notifier():
        calls.append("notification")
        raise RuntimeError("sensitive webhook configuration")

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        response = web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            after_user_saved=broken_notifier,
            post_response_runner=lambda *args, **kwargs: calls.append("post"),
        )
        frames = [f async for f in response.body_iterator]
        assert calls == ["notification", "post"]
        assert ports.operations.count("save_user") == 1
        assert len(session.history) == 2
        assert len(saved) == 1
        assert sum("[DONE]" in f for f in frames) == 1
        assert "sensitive webhook" not in caplog.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_post_response_rechecks_owner_when_it_changed_during_original_writer(
    monkeypatch,
):
    admission, session, _unused, opts, saved = persisted_fixture()
    ports = _chat_ports(opts)
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    jobs = []
    existing_writer = opts["persistence"].saver

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Saved before owner changed"}\n\n'
        yield "data: [DONE]\n\n"

    def mutated_saver(*args, **kwargs):
        original = existing_writer(*args, **kwargs)
        session.owner = "different-owner"
        return original

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        response = web_response(
            GatewayTurns(TurnService()), admission, session, opts,
            assistant_writer=mutated_saver,
            post_response_runner=lambda *args, **kwargs: jobs.append(True),
        )
        frames = [f async for f in response.body_iterator]
        assert len(saved) == 1
        assert jobs == []
        assert any("message_saved" in f for f in frames)
        assert sum("[DONE]" in f for f in frames) == 1
    finally:
        await cleanup_run(session.id)
