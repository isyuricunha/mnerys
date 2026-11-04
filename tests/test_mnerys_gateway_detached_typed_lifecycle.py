"""M2.1 typed full-turn detach/replay on the original Web run manager.

All model calls, session writes and credentials are synthetic. This is
process-local acceptance, not a network Gateway or durable event log.
"""

import asyncio
from dataclasses import replace

import pytest

from src import agent_runs
from src import turn_service as runtime
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedRunEvent
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_turn_composer import fixture
from tests.test_mnerys_gateway_text_persistence import (
    persisted_fixture, with_partial_writer,
)


async def clear_runs(*session_ids):
    for session_id in session_ids:
        run = agent_runs._RUNS.pop(session_id, None)
        if run is None:
            continue
        if run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        if run.evict_task is not None and not run.evict_task.done():
            run.evict_task.cancel()
            await asyncio.gather(run.evict_task, return_exceptions=True)


def start(gateway, admission, session, options):
    return gateway.open_composed_detached_text_events(
        admission, session, **options,
    )


@pytest.mark.asyncio
async def test_detached_typed_turn_replays_without_model_reexecution_or_terminal_race(
    monkeypatch,
):
    admission, session, ports, options, _ = persisted_fixture()
    gateway = GatewayTurns(TurnService())
    release = asyncio.Event()
    closed = []
    model_calls = []

    async def model(*args, **kwargs):
        model_calls.append(1)
        try:
            yield 'data: {"delta":"Saved answer"}\n\n'
            yield "data: [DONE]\n\n"
            await release.wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        delivery = start(gateway, admission, session, options)
        subscriber = delivery.events
        first = await asyncio.wait_for(anext(subscriber), timeout=3)
        assert isinstance(first, TypedRunEvent)
        assert first.run_id == delivery.run_id
        assert first.cursor == f"{delivery.run_id}:0"
        assert first.event.type == "message.delta"
        assert first.event.payload == {"text": "Saved answer"}
        await subscriber.aclose()
        assert agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        ) is not None

        # The persistent direct wrapper intentionally closes the original
        # model generator immediately after the confirmed assistant receipt.
        # A terminal already in the replay buffer must therefore always
        # correspond to the committed inherited writer.
        await asyncio.sleep(0)
        run = agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        )
        assert run is not None
        if any(item.type == "turn.completed" for item in run.buffer):
            assert session.messages[-1]["content"] == "Saved answer"

        release.set()
        resumed = gateway.resume_composed_detached_text_events(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=delivery.run_id,
            after_cursor=first.cursor,
        )
        assert resumed is not None
        assert resumed.outcome is delivery.outcome
        followup = [item async for item in resumed.events]
        await asyncio.wait_for(run.task, timeout=3)
        assert [item.event.type for item in followup] == [
            "message.final", "turn.completed",
        ]
        assert [item.event.sequence for item in [first, *followup]] == [
            1, 2, 3,
        ]
        assert followup[-1].cursor == f"{delivery.run_id}:2"
        assert closed == [True]
        assert model_calls == [1]
        assert delivery.outcome.state == "completed"
        assert session.messages[-1]["content"] == "Saved answer"

        again = gateway.resume_composed_detached_text_events(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=delivery.run_id,
            after_cursor=followup[-1].cursor,
        )
        assert again is not None
        assert [ev async for ev in again.events] == []
    finally:
        release.set()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_exact_run_stop_retains_one_partial_with_no_completion(
    monkeypatch,
):
    admission, session, ports, options, saved = persisted_fixture()
    partials = with_partial_writer(session, options)
    gateway = GatewayTurns(TurnService())
    gate = asyncio.Event()

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Visible partial."}\n\n'
        await gate.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        delivery = start(gateway, admission, session, options)
        first = await asyncio.wait_for(anext(delivery.events), timeout=3)
        assert first.event.type == "message.delta"
        assert gateway.stop_composed_detached_text_turn(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id="0" * 32,
        ) is False
        assert gateway.stop_composed_detached_text_turn(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=delivery.run_id,
        )
        run = agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        )
        await asyncio.wait_for(run.task, timeout=3)
        assert run.status == "stopped"
        assert delivery.outcome.state == "cancelled"
        assert partials == [(
            "Visible partial.",
            {"stopped": True, "interruption_reason": "cancelled"},
        )]
        assert saved == []
        assert all(event.type != "turn.completed" for event in run.buffer)
        assert len([item for item in session.messages if item["role"] == "assistant"]) == 1
        assert gateway.stop_composed_detached_text_turn(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=delivery.run_id,
        ) is False
    finally:
        gate.set()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_failed_generator_reports_only_sanitized_typed_failure(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    gateway = GatewayTurns(TurnService())

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Visible"}\n\n'
        raise RuntimeError("private upstream credential and diagnostics")

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        delivery = start(gateway, admission, session, options)
        result = [item async for item in delivery.events]
        run = agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        )
        await asyncio.wait_for(run.task, timeout=3)
        assert run.status == "error"
        assert [item.event.type for item in result] == [
            "message.delta", "turn.failed",
        ]
        assert result[-1].event.payload == {
            "code": "stream_exception", "status": None,
        }
        assert delivery.outcome.state == "failed"
        assert "private upstream" not in repr(result)
        assert all(isinstance(item, TurnEvent) for item in run.buffer)
        assert not session.messages
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_denied_admission_does_not_replace_live_typed_run(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    gateway = GatewayTurns(TurnService())
    gate = asyncio.Event()

    async def model(*args, **kwargs):
        yield 'data: {"delta":"Alive"}\n\n'
        await gate.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        delivery = start(gateway, admission, session, options)
        await asyncio.wait_for(anext(delivery.events), timeout=3)
        run = agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        )
        forged = replace(admission, _proof=None)
        with pytest.raises(AdmissionDenied):
            start(gateway, forged, session, options)
        with pytest.raises(AdmissionDenied):
            gateway.resume_composed_detached_text_events(
                forged, session,
                context_owner=options["context_owner"],
                expected_run_id=delivery.run_id,
            )
        with pytest.raises(AdmissionDenied):
            gateway.stop_composed_detached_text_turn(
                forged, session,
                context_owner=options["context_owner"],
                expected_run_id=delivery.run_id,
            )
        with pytest.raises(AdmissionDenied):
            gateway.stop_composed_detached_text_turn(
                admission, session, context_owner="mallory",
                expected_run_id=delivery.run_id,
            )
        assert agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        ) is run
        assert run.status == "running"
        gate.set()
        await asyncio.wait_for(run.task, timeout=3)
    finally:
        gate.set()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_replacement_stale_cursor_and_sse_modality_cannot_cross_replay(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    gateway = GatewayTurns(TurnService())
    model_started = asyncio.Event()
    gate = asyncio.Event()

    async def model(*args, **kwargs):
        model_started.set()
        yield 'data: {"delta":"First run"}\n\n'
        await gate.wait()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        old = start(gateway, admission, session, options)
        first = await asyncio.wait_for(anext(old.events), timeout=3)
        new = start(gateway, admission, session, options)
        assert old.run_id != new.run_id
        assert gateway.resume_composed_detached_text_events(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=old.run_id,
            after_cursor=first.cursor,
        ) is None
        assert gateway.stop_composed_detached_text_turn(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=old.run_id,
        ) is False
        run = agent_runs.get_matching_run(
            session.id, new.run_id, mode="typed",
        )
        assert run is not None
        with pytest.raises(ValueError, match="different event representation"):
            await anext(agent_runs.subscribe(session.id, run))
        assert gateway._turns.resume_active(session.id) is None

        latest_sub = new.events
        latest = await asyncio.wait_for(anext(latest_sub), timeout=3)
        assert latest.run_id == new.run_id
        with pytest.raises(ValueError, match="different run"):
            await anext(agent_runs.subscribe(
                session.id, run, after_cursor=first.cursor, typed=True,
            ))
        assert run.status == "running"
        gate.set()
        assert [ev async for ev in latest_sub][-1].event.type == "turn.completed"
        await asyncio.wait_for(run.task, timeout=3)
    finally:
        gate.set()
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_invalid_typed_run_cannot_cancel_existing_sse_run():
    session_id = "typed-preflight-exact-session"
    gate = asyncio.Event()

    async def original():
        yield 'data: {"delta":"original"}\n\n'
        await gate.wait()
        yield "data: [DONE]\n\n"

    try:
        run = agent_runs.start(session_id, original())
        first = await asyncio.wait_for(
            anext(agent_runs.subscribe(session_id, run)), timeout=3,
        )
        assert "original" in first
        with pytest.raises(TypeError):
            agent_runs.start(session_id, original(), mode="typed")
        assert agent_runs.get_matching_run(
            session_id, run.run_id, mode="sse",
        ) is run
        assert run.status == "running"
    finally:
        gate.set()
        await clear_runs(session_id)


@pytest.mark.asyncio
async def test_typed_terminal_and_cursor_are_held_until_writer_cleanup():
    session_id = "typed-terminal-order-session"
    outcome = TurnOutcome()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def source():
        try:
            outcome.state = "running"
            outcome.last_sequence = 1
            yield TurnEvent(1, "message.delta", {"text": "Safe"})
            outcome.last_sequence = 2
            yield TurnEvent(2, "message.final", {"text": "Safe"})
            outcome.state = "completed"
            outcome.last_sequence = 3
            yield TurnEvent(3, "turn.completed", {})
            await release.wait()
        finally:
            cleaned.set()

    turns = TurnService()
    try:
        delivery = turns.start_detached_typed(session_id, source(), outcome)
        first = await asyncio.wait_for(anext(delivery.events), timeout=3)
        assert first.event.type == "message.delta"
        run = agent_runs.get_matching_run(
            session_id, delivery.run_id, mode="typed",
        )
        await asyncio.sleep(0)
        assert not cleaned.is_set()
        assert run.status == "running"
        assert all(item.type != "turn.completed" for item in run.buffer)
        release.set()
        received = [item async for item in delivery.events]
        await asyncio.wait_for(run.task, timeout=3)
        assert cleaned.is_set()
        assert [item.event.type for item in received] == [
            "message.final", "turn.completed",
        ]
        assert [item.cursor for item in [first, *received]] == [
            f"{delivery.run_id}:0",
            f"{delivery.run_id}:1",
            f"{delivery.run_id}:2",
        ]
        assert delivery.outcome.state == "completed"
    finally:
        release.set()
        await clear_runs(session_id)


@pytest.mark.asyncio
async def test_immediate_stop_before_task_entry_never_starts_context_or_model(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    gateway = GatewayTurns(TurnService())
    calls = []

    async def model(*args, **kwargs):
        calls.append(True)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        delivery = start(gateway, admission, session, options)
        run = agent_runs.get_matching_run(
            session.id, delivery.run_id, mode="typed",
        )
        assert run is not None and not run.started
        assert gateway.stop_composed_detached_text_turn(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=delivery.run_id,
        )
        await asyncio.gather(run.task, return_exceptions=True)
        assert run.status == "stopped"
        assert delivery.outcome.state == "cancelled"
        assert ports.operations == []
        assert calls == []
        assert [ev async for ev in delivery.events] == []
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_rapid_replacement_before_task_entry_cancels_original_outcome(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    gateway = GatewayTurns(TurnService())
    calls = []

    async def model(*args, **kwargs):
        calls.append(True)
        yield 'data: {"delta":"New turn"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    try:
        old = start(gateway, admission, session, options)
        old_run = agent_runs.get_matching_run(
            session.id, old.run_id, mode="typed",
        )
        assert not old_run.started
        fresh = start(gateway, admission, session, options)
        assert old_run.status == "stopped"
        assert old.outcome.state == "cancelled"
        await asyncio.gather(old_run.task, return_exceptions=True)
        assert [e.event.type async for e in fresh.events] == [
            "message.delta", "message.final", "turn.completed",
        ]
        assert calls == [True]
        assert gateway.resume_composed_detached_text_events(
            admission, session,
            context_owner=options["context_owner"],
            expected_run_id=old.run_id,
        ) is None
    finally:
        await clear_runs(session.id)


@pytest.mark.asyncio
async def test_out_of_order_untrusted_typed_event_fails_closed_in_one_registry():
    session_id = "typed-out-of-order-session"
    outcome = TurnOutcome()

    async def bad():
        outcome.state = "running"
        yield TurnEvent(9, "message.delta", {"text": "unsafe sequence"})

    try:
        delivery = TurnService().start_detached_typed(
            session_id, bad(), outcome,
        )
        events = [entry async for entry in delivery.events]
        run = agent_runs.get_matching_run(
            session_id, delivery.run_id, mode="typed",
        )
        await run.task
        assert len(events) == 1
        assert events[0].event.type == "turn.failed"
        assert events[0].event.sequence == 1
        assert events[0].event.payload == {
            "code": "stream_exception", "status": None,
        }
        assert run.status == "error"
        assert outcome.state == "failed"
        assert not any(item.type == "message.delta" for item in run.buffer)
    finally:
        await clear_runs(session_id)


@pytest.mark.asyncio
async def test_exception_after_unpublished_typed_terminal_never_acknowledges_success():
    session_id = "typed-unwind-failure-session"
    outcome = TurnOutcome()

    async def fail_after_terminal():
        outcome.state = "running"
        outcome.last_sequence = 1
        yield TurnEvent(1, "message.delta", {"text": "Partial"})
        outcome.last_sequence = 2
        outcome.state = "completed"
        yield TurnEvent(2, "turn.completed", {})
        raise RuntimeError("private cleanup failure")

    try:
        delivery = TurnService().start_detached_typed(
            session_id, fail_after_terminal(), outcome,
        )
        events = [item async for item in delivery.events]
        run = agent_runs.get_matching_run(
            session_id, delivery.run_id, mode="typed",
        )
        await run.task
        assert [item.event.type for item in events] == [
            "message.delta", "turn.failed",
        ]
        assert events[-1].event.sequence == 2
        assert events[-1].event.payload == {
            "code": "stream_exception", "status": None,
        }
        assert outcome.state == "failed"
        assert outcome.last_sequence == 2
        assert "private cleanup" not in repr(events)
    finally:
        await clear_runs(session_id)


@pytest.mark.asyncio
async def test_invalid_typed_source_cannot_cancel_an_existing_web_sse_run():
    session_id = "typed-nonclosable-preflight-session"
    gate = asyncio.Event()

    async def web_source():
        yield 'data: {"delta":"web"}\n\n'
        await gate.wait()
        yield "data: [DONE]\n\n"

    try:
        web = agent_runs.start(session_id, web_source())
        await asyncio.wait_for(
            anext(agent_runs.subscribe(session_id, web)), timeout=3,
        )
        turns = TurnService()
        with pytest.raises(TypeError, match="fresh typed outcome"):
            turns.start_detached_typed(
                session_id, iter([TurnEvent(1, "turn.completed", {})]),
                TurnOutcome(),
            )
        assert agent_runs.get_matching_run(
            session_id, web.run_id, mode="sse",
        ) is web
        assert web.status == "running"

        invalid_outcome = TurnOutcome(state="completed")
        async def valid_source():
            yield TurnEvent(1, "turn.completed", {})
        stream = valid_source()
        try:
            with pytest.raises(TypeError, match="fresh typed outcome"):
                turns.start_detached_typed(session_id, stream, invalid_outcome)
        finally:
            await stream.aclose()
        assert web.status == "running"
    finally:
        gate.set()
        await clear_runs(session_id)
