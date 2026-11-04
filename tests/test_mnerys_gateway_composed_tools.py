"""One direct text-input composer through the inherited tool/authority pipeline."""

import asyncio
from dataclasses import replace
import json

import pytest

from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from src.agent_runtime.authority import ExactOperation
from src.agent_core_events import CoreToolEvent
from src.agent_tool_events import tool_event_frame
from tests.test_mnerys_gateway_turn_composer import fixture, gather


def configured(monkeypatch, credential='browser'):
    import src.tool_utils as utils
    import src.tool_security as security
    import src.agent_loop as loop
    monkeypatch.setattr(utils, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(security, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(loop, '_workspace_tools_disabled_for_owner', lambda owner: False)
    admission, session, ports, options = fixture(credential)
    options['request'] = TextTurnRequest('List my notes')
    options['settings'] = TrustedTextTurnSettings(offered_tools=frozenset({'manage_notes'}))
    return admission, session, ports, options


@pytest.mark.asyncio
@pytest.mark.parametrize('credential', ['browser', 'bearer', 'single_user'])
async def test_composer_builds_real_inventory_contract_and_bound_authority(monkeypatch, credential):
    import src.turn_service as module
    admission, session, ports, options = configured(monkeypatch, credential)
    seen = {}

    async def loop(url, model, messages, **kw):
        seen.update(kw)
        yield tool_event_frame({'type': 'tool_start', 'tool': 'manage_notes', 'command': 'PRIVATE'})
        yield tool_event_frame({'type': 'tool_output', 'tool': 'manage_notes', 'exit_code': 0, 'output': 'PRIVATE'})
        yield 'data: {"delta":"Three notes"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    events = [e async for e in delivery.events]
    assert [e.type for e in events] == ['tool.started', 'tool.completed', 'message.delta', 'message.final', 'turn.completed']
    assert 'PRIVATE' not in repr(events)
    assert delivery.outcome.content == 'Three notes'
    assert seen['turn_contract'].offered == frozenset({'manage_notes'})
    assert seen['turn_contract'].required_read_operation.tool == 'manage_notes'
    assert not seen['tool_policy'].blocks('manage_notes')
    assert seen['tool_policy'].blocks('bash')
    assert seen['tool_policy'].disable_mcp
    assert seen['request_authority'].bound_to(owner=admission.principal.owner_id, session_id=session.id)
    assert not seen['request_authority'].block_all
    assert not ports.private and not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['global', 'account', 'owner', 'guide'])
async def test_server_tool_surface_cannot_remove_any_existing_denial(monkeypatch, source):
    import src.turn_service as module
    import src.tool_security as security
    admission, session, ports, options = configured(monkeypatch)
    if source == 'global':
        options['settings'] = replace(options['settings'], global_disabled_tools=('manage_notes',))
    elif source == 'account':
        options['settings'] = replace(options['settings'], privileges={'can_use_agent': False})
    elif source == 'owner':
        monkeypatch.setattr(security, 'blocked_tools_for_owner', lambda owner: {'manage_notes'})
    else:
        options['request'] = TextTurnRequest('List my notes, but do not use any tools')
    seen = []

    async def loop(*args, **kw):
        seen.append(kw)
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    if source == 'account':
        from src.gateway_admission import AdmissionDenied
        with pytest.raises(AdmissionDenied):
            await gather(GatewayTurns(TurnService()).stream_composed_text_turn(admission, session, **options))
        assert not ports.operations and not seen
    else:
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(admission, session, **options))
        assert seen[0]['tool_policy'].blocks('manage_notes')
        assert not seen[0]['turn_contract'].permits('manage_notes')
        assert not seen[0]['request_authority'].permits(ExactOperation.normalize('manage_notes', '{"action":"list"}'))


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', [None, ['manage_notes'], frozenset({1}), frozenset({''}),
                                    frozenset({'mcp__email__send_email'}), frozenset({'host_shell'}),
                                    frozenset({'imaginary_tool'})])
async def test_invalid_or_unsupported_tool_surface_precedes_context_io(monkeypatch, surface):
    from src.gateway_admission import AdmissionDenied
    admission, session, ports, options = configured(monkeypatch)
    options['settings'] = replace(options['settings'], offered_tools=surface)
    with pytest.raises(AdmissionDenied):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(admission, session, **options))
    assert ports.operations == []
    assert session.messages == []


@pytest.mark.asyncio
async def test_foreign_session_fails_before_inventory_or_context(monkeypatch):
    import src.gateway_turn_composer as composer
    from src.gateway_admission import AdmissionDenied
    admission, session, ports, options = configured(monkeypatch)
    session.owner = 'mallory'

    def forbidden(*args, **kw):
        raise AssertionError('Foreign session reached tool inventory')
    monkeypatch.setattr(composer, 'prepare_admitted_tool_inventory', forbidden)
    with pytest.raises(AdmissionDenied):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(admission, session, **options))
    assert not ports.operations


@pytest.mark.asyncio
async def test_actual_agent_loop_executes_the_composed_immutable_read(monkeypatch):
    import src.agent_loop as loop
    from src.agent_runtime.authority import active_request_authority
    from src.turn_contract import active_turn_contract
    from tests.test_required_read_operation import notes_result

    admission, session, ports, options = configured(monkeypatch)
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *args, **kw: 10)
    calls = []

    async def provider(*args, **kw):
        yield 'data: {"delta":"I will list the notes."}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, **kw):
        authority = active_request_authority()
        assert authority.bound_to(owner='alice', session_id=session.id)
        assert authority.permits(ExactOperation.normalize(block.tool_type, block.content))
        assert not authority.permits(ExactOperation.normalize('manage_notes', '{"action":"add","title":"Unrequested"}'))
        assert active_turn_contract().permits(block.tool_type)
        assert not kw['tool_policy'].blocks(block.tool_type)
        assert json.loads(block.content)['action'] == 'list'
        calls.append(block.tool_type)
        return 'manage_notes: list', notes_result()

    monkeypatch.setattr(loop, 'stream_llm_with_fallback', provider)
    monkeypatch.setattr(loop, 'execute_tool_block', execute)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    events = [e async for e in delivery.events]
    assert calls == ['manage_notes']
    assert any(e.type == 'tool.started' for e in events)
    assert any(e.type == 'tool.completed' for e in events)
    assert delivery.outcome.state == 'completed'
    assert '#note-' in delivery.outcome.content
    assert session.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize('privileges,tool,message', [
    ({'can_use_bash': False}, 'bash', 'Run bash to print hello'),
    ({'can_manage_memory': False}, 'manage_memory', 'List my memories'),
    ({'can_use_browser': False}, 'private_browser', 'Browse https://example.com'),
])
async def test_offered_native_tool_cannot_bypass_its_account_limit(monkeypatch, privileges, tool, message):
    import src.turn_service as module
    admission, session, ports, options = configured(monkeypatch)
    options['request'] = TextTurnRequest(message)
    options['settings'] = TrustedTextTurnSettings(privileges=privileges, offered_tools=frozenset({tool}))
    seen = []

    async def loop(*args, **kw):
        seen.append(kw)
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(admission, session, **options))
    assert seen[0]['tool_policy'].blocks(tool)
    assert not seen[0]['turn_contract'].permits(tool)


@pytest.mark.asyncio
async def test_composed_tool_surface_rejects_native_terminal_before_io(monkeypatch):
    from src.gateway_admission import AdmissionDenied
    admission, session, ports, options = configured(monkeypatch)
    with pytest.raises(AdmissionDenied, match='tools-disabled'):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, native_terminal_events=True, **options,
        ))
    assert not ports.operations


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['complete', 'close', 'error', 'cancel'])
async def test_tool_profile_keeps_native_scope_and_contract_until_inner_cleanup(monkeypatch, ending):
    import src.turn_service as module
    from src.agent_tool_events import native_tool_events_enabled
    from src.turn_contract import active_turn_contract
    admission, session, ports, options = configured(monkeypatch)
    original = active_turn_contract()
    closed = []

    async def loop(*args, **kw):
        try:
            yield tool_event_frame({'type': 'tool_start', 'tool': 'manage_notes'})
            if ending == 'error':
                raise RuntimeError('synthetic error')
            if ending == 'cancel':
                raise asyncio.CancelledError()
            yield 'data: [DONE]\n\n'
        finally:
            closed.append((native_tool_events_enabled(), active_turn_contract().permits('manage_notes')))

    monkeypatch.setattr(module, 'stream_agent_loop', loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(admission, session, **options)
    assert (await anext(delivery.events)).type == 'tool.started'
    if ending == 'close':
        await delivery.events.aclose()
    elif ending in {'error', 'cancel'}:
        with pytest.raises(RuntimeError if ending == 'error' else asyncio.CancelledError):
            _ = [e async for e in delivery.events]
    else:
        assert [e.type async for e in delivery.events] == ['message.final', 'turn.completed']
    assert closed == [(True, True)]
    assert active_turn_contract() is original
    assert not native_tool_events_enabled()
    assert delivery.outcome.state == {
        'complete': 'completed', 'close': 'cancelled', 'error': 'failed', 'cancel': 'cancelled',
    }[ending]
    assert not session.messages
