"""M1: first reusable service seam, without a new Gateway or DB schema."""

import asyncio

import pytest

from src import agent_runs, turn_service as service_module
from src.tool_execution import AgentExecutionBridge, bind_execution_bridge, get_active_execution_bridge
from src.turn_contract import active_turn_contract, bind_turn_contract


@pytest.fixture
def isolated_runs(monkeypatch):
    """Avoid sharing the process-local registry with unrelated HTTP tests."""
    registry = {}
    monkeypatch.setattr(agent_runs, "_RUNS", registry)
    yield registry
    # All tests await their drain tasks; eviction timers are disposable.
    for run in registry.values():
        for task in (run.task, run.evict_task):
            if task is not None and not task.done():
                task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_bridge", [False, True])
@pytest.mark.parametrize("ending", ["complete", "close", "error", "cancel"])
async def test_agent_stream_reuses_loop_and_restores_context(
    monkeypatch, with_bridge, ending
):
    """No duplicate agent; contract and external bridge never leak between turns."""
    seen = []
    outer_contract = object()
    selected_contract = object()

    async def unused_tool(*args, **kwargs):
        raise AssertionError("no bridge execution is expected")

    outer_bridge = AgentExecutionBridge(unused_tool, frozenset({"bash"}), name="outer")
    inner_bridge = AgentExecutionBridge(unused_tool, frozenset({"web_search"}), name="inner")

    async def fake_loop(*args, **kwargs):
        assert args == ("model-url", "model-id", ["hello"])
        assert kwargs == {"turn_contract": selected_contract, "owner": "alice"}
        assert active_turn_contract() is selected_contract
        assert get_active_execution_bridge() is (inner_bridge if with_bridge else outer_bridge)
        try:
            yield "chunk"
            if ending == "error":
                raise RuntimeError("model failed")
            if ending == "cancel":
                raise asyncio.CancelledError()
        finally:
            seen.append((active_turn_contract(), get_active_execution_bridge()))

    monkeypatch.setattr(service_module, "stream_agent_loop", fake_loop)
    service = service_module.TurnService()

    with bind_turn_contract(outer_contract), bind_execution_bridge(outer_bridge):
        stream = service.stream_agent(
            inner_bridge if with_bridge else None,
            "model-url", "model-id", ["hello"],
            turn_contract=selected_contract, owner="alice",
        )
        try:
            assert await anext(stream) == "chunk"
            if ending == "close":
                await stream.aclose()
            else:
                with pytest.raises({
                    "complete": StopAsyncIteration,
                    "error": RuntimeError,
                    "cancel": asyncio.CancelledError,
                }[ending]):
                    await anext(stream)
            assert active_turn_contract() is outer_contract
            assert get_active_execution_bridge() is outer_bridge
        finally:
            await stream.aclose()

    assert seen == [
        (selected_contract, inner_bridge if with_bridge else outer_bridge)
    ]


@pytest.mark.asyncio
async def test_detached_turn_preserves_existing_frames_resume_and_completion(isolated_runs):
    """Service-level consumers get the same process-local replay as existing Web."""
    service = service_module.TurnService()
    first_frame = 'data: {"delta":"hello"}\n\n'
    terminal = "data: [DONE]\n\n"
    ready = asyncio.Event()

    async def source():
        yield first_frame
        await ready.wait()
        yield terminal

    session = "service-replay"
    stream = service.start_detached(session, source())
    assert stream.run_id == isolated_runs[session].run_id

    first = await anext(stream.events)
    assert first == first_frame
    resumed = service.resume_active(session)
    assert resumed is not None and resumed.run_id == stream.run_id

    ready.set()
    await isolated_runs[session].task
    assert [frame async for frame in stream.events] == [terminal]
    assert [frame async for frame in resumed.events] == [first_frame, terminal]
    assert service.resume_active(session) is None
    assert service.is_active(session) is False


@pytest.mark.asyncio
async def test_service_stop_and_finish_are_exact_run_scoped(isolated_runs):
    service = service_module.TurnService()
    gate = asyncio.Event()

    async def source():
        await gate.wait()
        yield "data: [DONE]\n\n"

    session = "service-cancel"
    handle = service.start_detached(session, source())
    await asyncio.sleep(0)
    assert service.is_active(session)
    assert not service.stop(session, None)
    assert not service.stop(session, "stale")
    assert not service.finish(session, "stale")
    assert service.finish(session, handle.run_id)
    assert agent_runs.should_finish(session)
    assert service.stop(session, handle.run_id)
    await asyncio.gather(isolated_runs[session].task, return_exceptions=True)
    assert isolated_runs[session].status == "stopped"
    assert service.resume_active(session) is None
    gate.set()


@pytest.mark.asyncio
async def test_replaced_turn_cannot_be_cancelled_with_old_service_handle(isolated_runs):
    service = service_module.TurnService()
    old_gate = asyncio.Event()
    new_gate = asyncio.Event()

    async def source(gate):
        await gate.wait()
        yield "data: [DONE]\n\n"

    session = "service-replaced"
    old = service.start_detached(session, source(old_gate))
    await asyncio.sleep(0)
    new = service.start_detached(session, source(new_gate))
    await asyncio.sleep(0)

    assert old.run_id != new.run_id
    assert not service.stop(session, old.run_id)
    assert not service.finish(session, old.run_id)
    assert service.resume_active(session).run_id == new.run_id
    assert service.stop(session, new.run_id)
    await asyncio.gather(isolated_runs[session].task, return_exceptions=True)
    old_gate.set()
    new_gate.set()


def test_turn_service_exposes_no_new_http_router_or_authentication():
    """This internal service is not yet a public authenticated Gateway."""
    assert not hasattr(service_module, "router")
    assert not hasattr(service_module.TurnService, "authenticate")
    assert service_module.turn_service.__class__ is service_module.TurnService
