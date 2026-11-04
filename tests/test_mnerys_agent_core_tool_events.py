"""Native tool observations preserve execution and sealed approval boundaries."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
import ast

import pytest

from src.agent_core_events import CoreApprovalEvent, CoreToolEvent
from src.agent_tool_events import (
    approval_requested_event, bind_native_tool_events,
    native_tool_events_enabled, tool_event_frame,
)
from src.gateway_turn_events import TurnOutcome, translate_legacy_turn_events


@pytest.mark.parametrize('kind', ['tool_start', 'tool_progress', 'tool_output'])
def test_web_bytes_and_raw_payload_are_preserved_by_default(kind):
    payload = dict(type=kind, tool='manage_notes', command='private command',
                   output='private output', tail='private progress', exit_code=0)
    assert tool_event_frame(payload) == 'data: ' + json.dumps(payload) + '\n\n'
    assert not native_tool_events_enabled()


@pytest.mark.parametrize('payload,expected', [
    ({'type': 'tool_start'}, 'tool.started'),
    ({'type': 'tool_progress'}, 'tool.progress'),
    ({'type': 'tool_output', 'exit_code': 0}, 'tool.completed'),
    ({'type': 'tool_output', 'exit_code': 1}, 'tool.failed'),
    ({'type': 'tool_output', 'error': 'private'}, 'tool.failed'),
    ({'type': 'tool_output', 'blocked': True}, 'tool.blocked'),
    ({'type': 'tool_output', 'execution_attempted': False}, 'tool.blocked'),
    ({'type': 'tool_output', 'execution_attempted': False,
      'ask_user': {'kind': 'tool_approval', 'approval_id': 'forged'}}, 'tool.awaiting_approval'),
])
def test_native_metadata_drops_all_tool_content_and_provider_ids(payload, expected):
    payload.update(tool='manage_notes', round=1, command='PRIVATE', output='PRIVATE',
                   tail='PRIVATE', call_id='PRIVATE', screenshot='PRIVATE', tool_args={'secret': 'PRIVATE'})
    with bind_native_tool_events(True):
        event = tool_event_frame(payload)
    assert isinstance(event, CoreToolEvent)
    assert event.type == expected
    assert event.tool == 'manage_notes'
    assert event.round == 1
    assert 'PRIVATE' not in repr(event)
    assert 'forged' not in repr(event)
    assert set(asdict(event)) == {'type', 'tool', 'round', 'exit_code'}


def test_authoritative_result_overrides_missing_observation_failure_metadata():
    payload = {'type': 'tool_output', 'tool': 'manage_notes', 'exit_code': 1}
    with bind_native_tool_events(True):
        assert tool_event_frame(payload, result={'blocked': True}).type == 'tool.blocked'
    assert tool_event_frame(payload, result={'blocked': True}) == 'data: ' + json.dumps(payload) + '\n\n'


@pytest.mark.asyncio
async def test_actual_required_read_branch_emits_native_tool_observations(monkeypatch):
    # The existing extracted branch executes the real dispatcher invocation and
    # verifies its unchanged policy/context. Run it off the active asyncio loop.
    from tests.test_required_read_operation import run_terminal_branch, notes_result

    def run():
        with bind_native_tool_events(True):
            return run_terminal_branch(monkeypatch, [], notes_result())
    chunks = await asyncio.to_thread(run)
    native = [chunk for chunk in chunks if isinstance(chunk, CoreToolEvent)]
    assert [chunk.type for chunk in native] == ['tool.started', 'tool.completed']
    assert all(chunk.tool == 'manage_notes' for chunk in native)
    assert not any(isinstance(chunk, str) and '"type": "tool_output"' in chunk for chunk in chunks)
    outcome = TurnOutcome()

    async def source():
        for chunk in chunks:
            yield chunk
    events = [event async for event in translate_legacy_turn_events(source(), outcome)]
    assert [event.type for event in events[:2]] == ['tool.started', 'tool.completed']
    assert outcome.state == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['complete', 'close', 'error', 'cancel'])
async def test_gateway_native_mode_scopes_the_real_turn_service(monkeypatch, ending):
    from tests.test_mnerys_gateway_agent_execution import setup
    from src import turn_service as module
    from src.gateway_turns import GatewayTurns
    from src.turn_service import TurnService

    admission, session, options = setup()
    cleanup = []
    waiting = asyncio.Event()

    async def loop(*args, **kw):
        assert native_tool_events_enabled()
        assert 'native_tool_events' not in kw
        try:
            yield tool_event_frame({'type': 'tool_start', 'tool': 'manage_notes'})
            if ending == 'error':
                raise RuntimeError('synthetic failure')
            if ending == 'cancel':
                waiting.set()
                await asyncio.Event().wait()
        finally:
            cleanup.append(native_tool_events_enabled())

    monkeypatch.setattr(module, 'stream_agent_loop', loop)

    async def run():
        stream = GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, native_tool_events=True, **options,
        )
        try:
            assert isinstance(await anext(stream), CoreToolEvent)
            if ending != 'close':
                try:
                    await anext(stream)
                except StopAsyncIteration:
                    pass
        finally:
            await stream.aclose()
            assert not native_tool_events_enabled()

    if ending == 'error':
        with pytest.raises(RuntimeError, match='synthetic'):
            await run()
    elif ending == 'cancel':
        task = asyncio.create_task(run())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await run()
    assert cleanup == [True]
    assert not native_tool_events_enabled()


@pytest.mark.asyncio
async def test_native_and_web_turns_are_isolated_between_tasks():
    async def run(enabled):
        with bind_native_tool_events(enabled):
            await asyncio.sleep(0)
            return tool_event_frame({'type': 'tool_start', 'tool': 'manage_notes'})
    native, legacy = await asyncio.gather(run(True), run(False))
    assert isinstance(native, CoreToolEvent)
    assert isinstance(legacy, str)


@pytest.mark.parametrize('decision', ['approve', 'approve_task', 'deny'])
def test_approval_events_come_from_owner_checked_consumption(decision):
    from tests.test_mnerys_gateway_approval_decisions import _pending, _decide, _admit
    from src.tool_approvals import ToolApprovalStore
    from src.gateway_admission import AdmissionDenied

    store = ToolApprovalStore()
    pending = _pending(store)
    requested = approval_requested_event(pending)
    assert requested == CoreApprovalEvent('approval.requested', pending.approval_id)
    with pytest.raises(AdmissionDenied):
        _decide(store, pending, admission=_admit(owner='bob'), decision=decision)
    assert store.peek(pending.approval_id) is pending
    result = _decide(store, pending, decision=decision)
    assert result.event == CoreApprovalEvent('approval.decided', pending.approval_id, decision)
    assert store.peek(pending.approval_id) is None
    assert (result.exact is None) == (decision == 'deny')
    assert 'content' not in asdict(result.event)
    with pytest.raises(AdmissionDenied):
        _decide(store, pending, decision=decision)
    with pytest.raises(TypeError):
        approval_requested_event({'approval_id': pending.approval_id})


@pytest.mark.asyncio
async def test_typed_approval_delivery_and_duplicate_done_filtering():
    from tests.test_mnerys_gateway_approval_decisions import _pending, _decide
    from src.tool_approvals import ToolApprovalStore
    store = ToolApprovalStore()
    pending = _pending(store)
    requested = approval_requested_event(pending)
    decided = _decide(store, pending, decision='deny').event

    async def source():
        yield requested
        yield decided
        yield 'data: [DONE]\n\n'
        yield requested
        yield 'data: [DONE]\n\n'
    outcome = TurnOutcome()
    events = [e async for e in translate_legacy_turn_events(source(), outcome)]
    assert [e.type for e in events] == [
        'approval.requested', 'approval.decided', 'message.final', 'turn.completed',
    ]
    assert events[1].payload == {'approval_id': pending.approval_id, 'decision': 'deny'}


def test_all_core_tool_serialization_sites_use_native_aware_emission():
    tree = ast.parse(Path('src/agent_loop.py').read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Yield):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == 'json'
                    and call.func.attr == 'dumps' and call.args):
                continue
            payload = call.args[0]
            if isinstance(payload, ast.Dict):
                kinds = [v.value for k, v in zip(payload.keys, payload.values)
                         if isinstance(k, ast.Constant) and k.value == 'type' and isinstance(v, ast.Constant)]
                assert not set(kinds) & {'tool_start', 'tool_progress', 'tool_output'}


@pytest.mark.asyncio
async def test_completion_and_teacher_wrappers_preserve_native_tool_observations():
    from src.agent_runtime.completion import with_completion_gate
    from src.teacher_escalation import with_teacher_takeover
    from src.agent_core_events import completed_core_event
    from tests.test_mnerys_gateway_approval_decisions import _pending
    from src.tool_approvals import ToolApprovalStore

    store = ToolApprovalStore()
    pending = _pending(store)
    tool = CoreToolEvent('tool.started', 'manage_notes')
    approval = approval_requested_event(pending)

    @with_teacher_takeover
    @with_completion_gate
    async def synthetic(endpoint_url, model, messages):
        yield tool
        yield approval
        yield completed_core_event()

    chunks = [chunk async for chunk in synthetic(
        'https://example.com/v1', 'test-model', [{'role': 'user', 'content': 'Hello'}],
    )]
    assert chunks[0] is tool
    assert chunks[1] is approval
    assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [
    CoreToolEvent('unknown', 'manage_notes'),
    CoreApprovalEvent('approval.requested', 'forged'),
    CoreApprovalEvent('approval.requested', 'a' * 43, 'approve'),
    CoreApprovalEvent('approval.decided', 'a' * 43, 'execute'),
])
async def test_invalid_native_metadata_is_rejected_without_reflecting_it(event):
    async def source():
        yield event
    outcome = TurnOutcome()
    with pytest.raises(ValueError, match='Invalid native Core'):
        _ = [e async for e in translate_legacy_turn_events(source(), outcome)]
    assert outcome.state == 'failed'
    assert outcome.failure_code == 'stream_exception'


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', [None, 1, 'true', {}])
async def test_gateway_rejects_nonboolean_native_mode_before_loop(monkeypatch, flag):
    from tests.test_mnerys_gateway_agent_execution import setup
    from src.gateway_admission import AdmissionDenied
    from src.gateway_turns import GatewayTurns
    from src.turn_service import TurnService
    admission, session, options = setup()
    with pytest.raises(AdmissionDenied, match='native tool routing flag'):
        _ = [e async for e in GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, native_tool_events=flag, **options,
        )]


@pytest.mark.asyncio
@pytest.mark.parametrize('paused', [False, True])
async def test_native_teacher_preserves_private_audit_and_approval_pause(monkeypatch, paused):
    import src.agent_loop as loop
    import src.teacher_escalation as teacher
    import src.settings as settings
    import src.ai_interaction as interaction
    from tests.test_mnerys_gateway_approval_decisions import _pending
    from src.tool_approvals import ToolApprovalStore

    pending = _pending(ToolApprovalStore())
    trace = {'tool': 'manage_notes', 'command': 'private-command',
             'output': 'private-output', 'exit_code': 1}
    evaluated = []

    def evaluate(events, text):
        evaluated.append(events)
        return 'failure', 'synthetic failure'

    async def synthetic_loop(**kwargs):
        yield tool_event_frame({'type': 'tool_output', **trace})
        if paused:
            yield approval_requested_event(pending)
        yield 'data: ' + json.dumps({'type': 'metrics', 'data': {'tool_events': [trace]}}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: {
        'teacher_enabled': True, 'teacher_model': 'test-teacher',
    }.get(key, default))
    monkeypatch.setattr(interaction, '_resolve_model', lambda *a, **kw: (
        'https://example.com/v1', 'test-model', {},
    ))
    monkeypatch.setattr(teacher, 'evaluate_turn_regex', evaluate)
    monkeypatch.setattr(loop, 'stream_agent_loop', synthetic_loop)
    with bind_native_tool_events(True):
        chunks = [e async for e in teacher.run_teacher_inline(
            student_endpoint_url='https://example.com/v1',
            student_messages=[{'role': 'user', 'content': 'Hello'}],
            student_tool_events=[], student_reply='Failed', owner='alice',
        )]
    assert any(isinstance(e, CoreToolEvent) for e in chunks)
    if paused:
        assert len(evaluated) == 1
        assert any(isinstance(e, CoreApprovalEvent) for e in chunks)
    else:
        assert len(evaluated) == 2
        assert evaluated[1][0]['command'] == 'private-command'
        assert evaluated[1][0]['output'] == 'private-output'
    assert all('private-' not in repr(e) for e in chunks if isinstance(e, CoreToolEvent))
