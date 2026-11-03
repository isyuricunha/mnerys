"""Regression tests for run-scoped, opt-in SSE replay cursors.

These are process-local cursors, not durable Gateway event storage.
"""

import asyncio

import pytest

from src import agent_runs


async def _read_all(stream):
    return [event async for event in stream]


def _cleanup(*session_ids):
    for session_id in session_ids:
        run = agent_runs._RUNS.pop(session_id, None)
        if run is None:
            continue
        for task in (run.evict_task, run.task):
            if task is not None and not task.done():
                task.cancel()


def test_legacy_sse_frames_are_unchanged_by_default():
    async def scenario():
        session_id = "cursor-legacy-session"

        async def stream():
            yield 'event: message\ndata: {"delta":"a"}\n\n'
            yield "data: [DONE]\n\n"

        try:
            run = agent_runs.start(session_id, stream())
            await run.task
            assert await _read_all(agent_runs.subscribe(session_id, run)) == [
                'event: message\ndata: {"delta":"a"}\n\n',
                "data: [DONE]\n\n",
            ]
        finally:
            _cleanup(session_id)

    asyncio.run(scenario())


def test_resume_after_last_acknowledged_event_without_duplicates():
    async def scenario():
        session_id = "cursor-resume-session"
        gate = asyncio.Event()

        async def stream():
            yield 'data: {"delta":"a"}\n\n'
            await gate.wait()
            yield 'data: {"delta":"b"}\n\n'
            yield "data: [DONE]\n\n"

        try:
            run = agent_runs.start(session_id, stream())
            subscription = agent_runs.subscribe(session_id, run, with_event_ids=True)
            first = await subscription.__anext__()
            assert first == f'id: {run.run_id}:0\ndata: {{"delta":"a"}}\n\n'
            await subscription.aclose()
            gate.set()
            await run.task

            cursor = first.splitlines()[0][4:]
            replay = await _read_all(
                agent_runs.subscribe(
                    session_id, run, after_cursor=cursor, with_event_ids=True
                )
            )
            assert replay == [
                f'id: {run.run_id}:1\ndata: {{"delta":"b"}}\n\n',
                f"id: {run.run_id}:2\ndata: [DONE]\n\n",
            ]
            assert await _read_all(
                agent_runs.subscribe(
                    session_id, run, after_cursor=f"{run.run_id}:2",
                    with_event_ids=True,
                )
            ) == []
        finally:
            gate.set()
            _cleanup(session_id)

    asyncio.run(scenario())


def test_cursor_reconnect_continues_live_stream_in_order():
    async def scenario():
        session_id = "cursor-live-session"
        gate = asyncio.Event()

        async def stream():
            yield "data: first\n\n"
            await gate.wait()
            yield "data: second\n\n"
            yield "data: [DONE]\n\n"

        try:
            run = agent_runs.start(session_id, stream())
            first_sub = agent_runs.subscribe(session_id, run, with_event_ids=True)
            first = await first_sub.__anext__()
            assert first == f"id: {run.run_id}:0\ndata: first\n\n"
            await first_sub.aclose()

            second_sub = agent_runs.subscribe(
                session_id,
                run,
                after_cursor=f"{run.run_id}:0",
                with_event_ids=True,
            )
            pending = asyncio.create_task(second_sub.__anext__())
            await asyncio.sleep(0)
            gate.set()
            assert await asyncio.wait_for(pending, timeout=2) == (
                f"id: {run.run_id}:1\ndata: second\n\n"
            )
            assert await _read_all(second_sub) == [
                f"id: {run.run_id}:2\ndata: [DONE]\n\n"
            ]
            await run.task
        finally:
            gate.set()
            _cleanup(session_id)

    asyncio.run(scenario())


def test_foreign_session_run_is_rejected():
    async def scenario():
        session_a, session_b = "cursor-session-a", "cursor-session-b"

        async def stream():
            yield "data: [DONE]\n\n"

        try:
            run = agent_runs.start(session_a, stream())
            await run.task
            with pytest.raises(ValueError, match="different session"):
                await _read_all(agent_runs.subscribe(session_b, run))
        finally:
            _cleanup(session_a, session_b)

    asyncio.run(scenario())


def test_stale_run_cursor_does_not_replay_replacement_run():
    async def scenario():
        session_id = "cursor-replaced-session"

        async def stream():
            yield "data: [DONE]\n\n"

        try:
            old = agent_runs.start(session_id, stream())
            await old.task
            new = agent_runs.start(session_id, stream())
            await new.task
            assert old.run_id != new.run_id
            with pytest.raises(ValueError, match="different run"):
                await _read_all(
                    agent_runs.subscribe(
                        session_id,
                        new,
                        after_cursor=f"{old.run_id}:0",
                        with_event_ids=True,
                    )
                )
            # A previously constructed response may still finish replaying
            # the exact old run rather than swapping to the new one.
            assert await _read_all(
                agent_runs.subscribe(
                    session_id, old, after_cursor=f"{old.run_id}:0"
                )
            ) == []
        finally:
            _cleanup(session_id)

    asyncio.run(scenario())


@pytest.mark.parametrize("cursor", [
    "",
    "invalid",
    "abc:0",
    "0" * 32 + ":-1",
    "0" * 32 + ":01",
    "0" * 32 + ":0:1",
    "x" * 200,
    123,
])
def test_malformed_cursor_is_rejected(cursor):
    async def scenario():
        session_id = "cursor-bad-format-session"

        async def stream():
            yield "data: [DONE]\n\n"

        try:
            run = agent_runs.start(session_id, stream())
            await run.task
            with pytest.raises(ValueError, match="cursor"):
                await _read_all(
                    agent_runs.subscribe(session_id, run, after_cursor=cursor)
                )
            with pytest.raises(ValueError, match="ahead"):
                await _read_all(
                    agent_runs.subscribe(
                        session_id, run, after_cursor=f"{run.run_id}:999"
                    )
                )
        finally:
            _cleanup(session_id)

    asyncio.run(scenario())


def test_cursor_for_evicted_run_is_not_silently_accepted():
    async def scenario():
        with pytest.raises(ValueError, match="no longer available"):
            await _read_all(
                agent_runs.subscribe(
                    "cursor-missing-session",
                    after_cursor=f"{'0' * 32}:0",
                    with_event_ids=True,
                )
            )

    asyncio.run(scenario())
