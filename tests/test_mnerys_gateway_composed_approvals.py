"""Direct approval control resumes the sealed action, never button-derived intent."""

import asyncio
from dataclasses import replace
import json

import pytest

from src.agent_runtime.authority import RequestAuthority, ExactOperation
from src.agent_tool_events import tool_event_frame
from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal, SessionOwnership
from src.gateway_turn_composer import ToolApprovalTurnRequest, TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured


def setup(monkeypatch, *, choice='approve_task', owner='alice', session_id='session-1', **pending_options):
    admission, session, ports, options = configured(monkeypatch)
    store = ToolApprovalStore()
    content = '{"action":"list"}'
    params = dict(
        owner=owner, session_id=session_id, origin_run_id='fixture-run',
        tool_name='manage_notes', content=content, workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action('manage_notes', content),
        selected_tools={'manage_notes'}, continuation_query='List my notes',
        request_text='List my notes',
        request_authority=RequestAuthority.empty(owner=owner, session_id=session_id),
    )
    params.update(pending_options)
    pending = store.create(**params)
    options.update(approval_store=store, request=ToolApprovalTurnRequest(pending.approval_id, choice))
    return admission, session, ports, options, store, pending


async def collect(admission, session, options):
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    events = [e async for e in delivery.events]
    return delivery, events


@pytest.mark.asyncio
@pytest.mark.parametrize('choice,scope', [('approve', 'chat_session'), ('approve_task', 'task')])
async def test_control_restores_sealed_action_authority_and_scope(monkeypatch, choice, scope):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch, choice=choice)
    seen = []

    async def loop(url, model, messages, **kw):
        seen.append(kw)
        exact = kw['exact_approval']
        assert exact.pending is pending
        assert exact.scope.value == scope
        assert kw['external_untrusted_context_seen'] is True
        assert kw['request_authority'].grants == pending.request_authority.grants == ()
        assert kw['request_authority'].bound_to(owner='alice', session_id=session.id)
        assert not kw['request_authority'].permits(ExactOperation.normalize('bash', 'echo unrequested'))
        assert kw['turn_contract'].offered == frozenset({'manage_notes'})
        assert kw['relevant_tools'] == {'manage_notes'}
        assert messages[-1]['content'] == pending.continuation_query
        assert all(item.get('content') not in {'approve', 'approve_task', 'deny'} for item in messages)
        assert exact.claim(owner='alice', session_id=session.id, tool_name=pending.tool_name,
                           content=pending.content, workspace=None)
        assert not exact.claim(owner='alice', session_id=session.id, tool_name=pending.tool_name,
                               content=pending.content, workspace=None)
        yield tool_event_frame({'type': 'tool_output', 'tool': 'manage_notes', 'exit_code': 0})
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    delivery, events = await collect(admission, session, options)
    assert len(seen) == 1
    assert events[0].type == 'approval.decided'
    assert events[0].payload == {'approval_id': pending.approval_id, 'decision': choice}
    assert delivery.outcome.state == 'completed'
    assert store.peek(pending.approval_id) is None
    assert not session.messages
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_denial_consumes_without_context_provider_or_tools(monkeypatch):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch, choice='deny')
    options['settings'] = TrustedTextTurnSettings(privileges={'can_use_agent': False})

    async def impossible(*args, **kw):
        raise AssertionError('Denial executed the Agent Loop')
        yield
    monkeypatch.setattr(module, 'stream_agent_loop', impossible)
    delivery, events = await collect(admission, session, options)
    assert [e.type for e in events] == ['approval.decided', 'message.final', 'turn.completed']
    assert events[0].payload['decision'] == 'deny'
    assert delivery.outcome.content == 'The action was denied.'
    assert not ports.operations and not session.messages
    assert store.peek(pending.approval_id) is None
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['owner', 'session', 'delegated', 'bad_choice', 'workspace',
                                 'document', 'tampered', 'expired', 'global', 'account', 'surface',
                                 'origin_denial', 'origin_blockall'])
async def test_invalid_continuation_never_reaches_context_or_claims_action(monkeypatch, case):
    extra = {}
    if case in {'origin_denial', 'origin_blockall'}:
        extra['request_authority'] = replace(
            RequestAuthority.empty(owner='alice', session_id='session-1'),
            denied=frozenset({'manage_notes'}) if case == 'origin_denial' else frozenset(),
            block_all=case == 'origin_blockall',
        )
    if case == 'owner': extra['owner'] = 'mallory'
    if case == 'session': extra['session_id'] = 'other-thread'
    if case == 'workspace': extra['workspace'] = '/unsupported-example'
    if case == 'document': extra.update(document_id='example-document', document_version=1)
    admission, session, ports, options, store, pending = setup(monkeypatch, **extra)
    if case == 'delegated':
        admission = GatewayAdmission().admit(
            Principal('alice', 'bearer', frozenset({'chat'})),
            session.id, lambda _: SessionOwnership('alice'),
        )
    if case == 'bad_choice': options['request'] = ToolApprovalTurnRequest(pending.approval_id, 'execute-anything')
    if case == 'tampered': object.__setattr__(pending, 'content', '{"action":"add"}')
    if case == 'expired': object.__setattr__(pending, 'expires_at', 0)
    if case == 'global': options['settings'] = replace(options['settings'], global_disabled_tools=('manage_notes',))
    if case == 'account': options['settings'] = replace(options['settings'], privileges={'can_use_agent': False})
    if case == 'surface': options['settings'] = TrustedTextTurnSettings()
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert not ports.operations and not session.messages
    if case != 'expired':
        assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
async def test_context_failure_keeps_pending_action_available(monkeypatch):
    admission, session, ports, options, store, pending = setup(monkeypatch)

    async def fail(*args, **kw):
        raise RuntimeError('synthetic context failure')
    monkeypatch.setattr(ports, 'preprocess_message', fail)
    with pytest.raises(RuntimeError, match='context failure'):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
async def test_expiry_during_context_is_rechecked_before_execution(monkeypatch):
    admission, session, ports, options, store, pending = setup(monkeypatch)
    original = ports.preprocess_message

    async def expire(*args, **kw):
        result = await original(*args, **kw)
        object.__setattr__(pending, 'expires_at', 0)
        return result
    monkeypatch.setattr(ports, 'preprocess_message', expire)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_cancel_after_decision_does_not_reissue_unexecuted_grant(monkeypatch):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch)
    executed = []

    async def loop(*args, **kw):
        executed.append(True)
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    assert (await anext(delivery.events)).type == 'approval.decided'
    await delivery.events.aclose()
    assert delivery.outcome.state == 'cancelled'
    assert not executed
    assert store.peek(pending.approval_id) is None
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)


@pytest.mark.asyncio
async def test_concurrent_continuations_execute_at_most_once(monkeypatch):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch)
    original = ports.preprocess_message
    executed = []

    async def pause(*args, **kw):
        await asyncio.sleep(0)
        return await original(*args, **kw)

    async def loop(*args, **kw):
        executed.append(kw['exact_approval'])
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(ports, 'preprocess_message', pause)
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    results = await asyncio.gather(collect(admission, session, options), collect(admission, session, options),
                                   return_exceptions=True)
    assert len(executed) == 1
    assert sum(isinstance(result, AdmissionDenied) for result in results) == 1
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_ordinary_user_turn_retires_card_and_preserves_external_taint(monkeypatch):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch)
    options['request'] = TextTurnRequest('Explain queues instead')
    seen = []

    async def loop(*args, **kw):
        seen.append(kw)
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    await collect(admission, session, options)
    assert store.peek(pending.approval_id) is None
    assert seen[0]['external_untrusted_context_seen'] is True
    assert 'exact_approval' not in seen[0]


@pytest.mark.asyncio
async def test_real_loop_and_dispatcher_claim_only_the_sealed_action(monkeypatch):
    import src.agent_loop as loop
    import src.tool_execution as execution
    from src.agent_runtime.authority import active_request_authority
    from src.tool_approvals import active_tool_approval_store, tool_approval_store
    from tests.test_required_read_operation import notes_result

    admission, session, ports, options, store, pending = setup(monkeypatch)
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *args, **kw: 10)
    calls = []

    async def notes(content, **kw):
        assert active_tool_approval_store(tool_approval_store) is store
        assert active_request_authority().grants == pending.request_authority.grants
        assert content == pending.content
        calls.append((pending.tool_name, content))
        return notes_result()

    async def model(*args, **kw):
        yield 'data: {"delta":"The notes are listed."}\n\n'
        yield 'data: [DONE]\n\n'
    dispatches = []

    async def dispatch(*args, **kw):
        result = await execution.execute_tool_block(*args, **kw)
        dispatches.append(result)
        return result
    monkeypatch.setattr(loop, 'execute_tool_block', dispatch)
    import src.tool_implementations as actions
    monkeypatch.setattr(actions, 'do_manage_notes', notes)
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', model)
    delivery, events = await collect(admission, session, options)
    assert len(calls) == 1, (delivery.outcome.content, dispatches)
    assert events[0].type == 'approval.decided'
    assert any(e.type == 'tool.completed' for e in events)
    assert delivery.outcome.state == 'completed'
    assert store.peek(pending.approval_id) is None
    assert active_tool_approval_store(tool_approval_store) is tool_approval_store
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_while_preparing_context_preserves_the_pending_action(monkeypatch):
    admission, session, ports, options, store, pending = setup(monkeypatch)
    entered = asyncio.Event()

    async def pause(*args, **kw):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ports, 'preprocess_message', pause)
    task = asyncio.create_task(collect(admission, session, options))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
async def test_stored_originating_message_is_not_duplicated(monkeypatch):
    import src.turn_service as module
    admission, session, ports, options, store, pending = setup(monkeypatch)
    session.messages = [{'role': 'user', 'content': pending.continuation_query}]
    original = list(session.messages)

    async def loop(url, model, messages, **kw):
        assert sum(m.get('content') == pending.continuation_query for m in messages) == 1
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    await collect(admission, session, options)
    assert session.messages == original


@pytest.mark.asyncio
async def test_real_request_approval_resume_round_trip_uses_one_store_and_one_effect(monkeypatch):
    import src.agent_loop as loop
    import src.tool_implementations as actions
    from src.tool_approvals import active_tool_approval_store, tool_approval_store
    admission, session, ports, options, store, retired = setup(monkeypatch)
    options['request'] = TextTurnRequest('Add a note titled Example with content Hello.')
    content = json.dumps({'action': 'add', 'title': 'Example', 'content': 'Hello'})
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *args, **kw: 10)
    effects = []

    async def notes(command, **kw):
        assert active_tool_approval_store(tool_approval_store) is store
        assert json.loads(command) == json.loads(content)
        effects.append(command)
        return {'output': 'Example note created.', 'exit_code': 0, 'note_id': 'example-note'}

    async def model(*args, **kw):
        yield 'data: ' + json.dumps({'type': 'tool_calls', 'calls': [
            {'id': 'example-call', 'name': 'manage_notes', 'arguments': content},
        ]}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(actions, 'do_manage_notes', notes)
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', model)
    first_delivery, requested_events = await collect(admission, session, options)
    assert first_delivery.outcome.state == "awaiting_approval"
    assert requested_events[-1].type == "turn.awaiting_approval"
    assert not effects
    assert store.peek(retired.approval_id) is None
    requested = next(e for e in requested_events if e.type == 'approval.requested')
    pending = store.peek(requested.payload['approval_id'])
    assert pending is not None
    assert pending.tool_name == 'manage_notes'
    assert json.loads(pending.content) == json.loads(content)
    options['request'] = ToolApprovalTurnRequest(pending.approval_id, 'approve_task')
    delivery, events = await collect(admission, session, options)
    assert len(effects) == 1
    assert delivery.outcome.state == 'completed'
    assert any(e.type == 'tool.completed' for e in events)
    assert store.peek(pending.approval_id) is None
    assert active_tool_approval_store(tool_approval_store) is tool_approval_store
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert len(effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['complete', 'close', 'error', 'cancel'])
async def test_approval_store_binding_survives_cleanup_and_never_leaks(monkeypatch, ending):
    import src.turn_service as module
    from src.tool_approvals import active_tool_approval_store, tool_approval_store
    admission, session, ports, options, store, pending = setup(monkeypatch)
    closed = []

    async def loop(*args, **kw):
        try:
            assert active_tool_approval_store(tool_approval_store) is store
            yield tool_event_frame({'type': 'tool_start', 'tool': 'manage_notes'})
            if ending == 'error':
                raise RuntimeError('synthetic runtime error')
            if ending == 'cancel':
                raise asyncio.CancelledError()
            yield 'data: [DONE]\n\n'
        finally:
            closed.append(active_tool_approval_store(tool_approval_store) is store)

    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    assert (await anext(delivery.events)).type == 'approval.decided'
    assert (await anext(delivery.events)).type == 'tool.started'
    if ending == 'close':
        await delivery.events.aclose()
    elif ending in {'error', 'cancel'}:
        with pytest.raises(RuntimeError if ending == 'error' else asyncio.CancelledError):
            _ = [e async for e in delivery.events]
    else:
        _ = [e async for e in delivery.events]
    assert closed == [True]
    assert active_tool_approval_store(tool_approval_store) is tool_approval_store


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_share_the_bound_approval_store(monkeypatch):
    import src.turn_service as module
    from src.tool_approvals import active_tool_approval_store, tool_approval_store
    first = setup(monkeypatch)
    second = setup(monkeypatch)
    expected = {first[5].approval_id: first[4], second[5].approval_id: second[4]}

    async def loop(*args, **kw):
        store = expected[kw['exact_approval'].pending.approval_id]
        assert active_tool_approval_store(tool_approval_store) is store
        await asyncio.sleep(0)
        assert active_tool_approval_store(tool_approval_store) is store
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    await asyncio.gather(collect(first[0], first[1], first[3]), collect(second[0], second[1], second[3]))
    assert active_tool_approval_store(tool_approval_store) is tool_approval_store


@pytest.mark.asyncio
@pytest.mark.parametrize('native_done', [False, True])
async def test_pending_native_approval_is_a_pause_not_task_completion(monkeypatch, native_done):
    from src.agent_core_events import completed_core_event
    from src.agent_tool_events import approval_requested_event
    from src.gateway_turn_events import TurnOutcome, translate_legacy_turn_events
    _, _, _, _, _, pending = setup(monkeypatch)

    async def source():
        yield approval_requested_event(pending)
        yield completed_core_event() if native_done else 'data: [DONE]\n\n'
        yield 'data: [DONE]\n\n'
        yield approval_requested_event(pending)
    outcome = TurnOutcome()
    events = [e async for e in translate_legacy_turn_events(source(), outcome)]
    assert outcome.state == 'awaiting_approval'
    assert outcome.failure_code is None
    assert [e.type for e in events] == ['approval.requested', 'message.final', 'turn.awaiting_approval']
    assert events[-1].payload == {'approval_id': pending.approval_id}
