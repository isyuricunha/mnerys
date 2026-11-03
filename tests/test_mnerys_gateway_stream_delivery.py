"""M2.1: admitted stream delivery uses one run manager and no Web imports."""
import ast
import asyncio
import inspect
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import agent_runs
from src.gateway_admission import (
    AdmissionDenied, AdmittedSession, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_stream_delivery import (
    TurnDelivery, open_admitted_stream, should_detach_stream,
)
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService, TurnStream


def _admission(session_id="stream-a", *, owner="alice", kind="browser"):
    scopes=frozenset({"chat"}) if kind=="bearer" else frozenset()
    principal = Principal(owner if kind != "single_user" else None, kind, scopes)
    return GatewayAdmission().admit(
        principal, session_id,
        lambda _: SessionOwnership(owner_id=owner),
    )


async def _events():
    yield 'data: {"delta":"hi"}\n\n'
    yield 'data: [DONE]\n\n'


@pytest.mark.parametrize("compare_mode,context,expected", [
    (False, None, True),
    (False, {}, True),
    (False, {"surface": "odysseus-tui", "unattended_mode": True}, True),
    (True, {}, False),
    (True, {"surface": "odysseus-native"}, False),
    (False, {"surface": "odysseus-native", "unattended_mode": True}, False),
    (False, {"surface": "odysseus-native", "unattended_mode": "true"}, True),
    (False, "forged", True),
])
def test_stream_lifecycle_policy_preserves_legacy_modes(compare_mode, context, expected):
    assert should_detach_stream(
        compare_mode=compare_mode, client_runtime_context=context,
    ) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    {},
    {"surface": "odysseus-native", "unattended_mode": True},
])
async def test_direct_stream_never_registers_detached_run(context):
    mock_turns=MagicMock()
    gateway=GatewayTurns(mock_turns)
    source=_events()
    delivery=gateway.open_stream(
        _admission(), source, compare_mode=True, client_runtime_context=context,
    )
    assert delivery == TurnDelivery(events=source, detached=False)
    assert [event async for event in delivery.events] == [
        'data: {"delta":"hi"}\n\n', 'data: [DONE]\n\n',
    ]
    mock_turns.start_detached.assert_not_called()


@pytest.mark.asyncio
async def test_native_unattended_stream_stays_direct():
    turns=MagicMock()
    gateway=GatewayTurns(turns)
    source=_events()
    delivered=gateway.open_stream(
        _admission(), source, compare_mode=False,
        client_runtime_context={"surface": "odysseus-native", "unattended_mode": True},
    )
    assert delivered.detached is False
    assert delivered.run_id is None
    assert delivered.events is source
    turns.start_detached.assert_not_called()
    await source.aclose()


@pytest.mark.asyncio
async def test_detached_stream_keeps_original_service_and_exact_run_resume():
    session_id="mnerys-gateway-stream-delivery"
    agent_runs._RUNS.pop(session_id, None)
    gate=asyncio.Event()
    calls=0

    async def source():
        nonlocal calls
        calls+=1
        yield 'data: {"delta":"first"}\n\n'
        await gate.wait()
        yield 'data: [DONE]\n\n'

    gateway=GatewayTurns(TurnService())
    grant=_admission(session_id,kind="bearer")
    delivery=gateway.open_stream(
        grant, source(), compare_mode=False, client_runtime_context={},
    )
    assert delivery.detached is True
    assert delivery.run_id
    first=await asyncio.wait_for(anext(delivery.events),timeout=2)
    assert first == 'data: {"delta":"first"}\n\n'
    resumed=gateway.resume_active(grant)
    assert isinstance(resumed, TurnStream)
    assert resumed.run_id==delivery.run_id
    assert gateway.stop(grant, "wrong-run-id") is False
    assert gateway.stop(grant, None) is False
    gate.set()
    assert [event async for event in delivery.events] == ['data: [DONE]\n\n']
    await asyncio.wait_for(agent_runs._RUNS[session_id].task,timeout=2)
    assert calls==1


@pytest.mark.parametrize("forged", [
    None,
    "session-id",
    AdmittedSession(Principal("alice","browser"),"stream-a"),
])
def test_no_lifecycle_dispatch_without_issued_proof(forged):
    turns=MagicMock()
    stream=_events()
    with pytest.raises(AdmissionDenied) as raised:
        open_admitted_stream(
            forged, turns, stream,
            compare_mode=False, client_runtime_context={},
        )
    assert raised.value.status_code==403
    turns.start_detached.assert_not_called()


def test_tampered_grant_denied_before_direct_stream_handoff():
    original=_admission()
    fake=replace(original,session_id="foreign")
    turns=MagicMock()
    with pytest.raises(AdmissionDenied):
        GatewayTurns(turns).open_stream(
            fake, _events(), compare_mode=True, client_runtime_context={},
        )
    assert turns.method_calls == []


def test_stream_service_has_no_route_or_fastapi_imports():
    from src import gateway_stream_delivery
    tree=ast.parse(inspect.getsource(gateway_stream_delivery))
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            assert not (node.module or "").startswith(("routes","fastapi"))
        if isinstance(node,ast.Import):
            assert all(not alias.name.startswith(("routes","fastapi"))
                       for alias in node.names)
