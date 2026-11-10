"""Filtered native tool observations; formatting never grants execution authority.

Only the admitted, in-process runtime opts in. Raw tool arguments, outputs,
progress tails and provider call IDs stay out of this metadata-only contract.
The legacy Web serializer remains byte-compatible when the mode is disabled.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import re

from src.agent_core_events import CoreApprovalEvent, CoreToolEvent

_native_tools: ContextVar[bool] = ContextVar('native_core_tool_events', default=False)
_observer = ContextVar('core_tool_observer', default=None)
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z')


def native_tool_events_enabled() -> bool:
    return _native_tools.get()


@contextmanager
def bind_native_tool_events(enabled: bool):
    """Scope formatting to one runtime, restoring it even on cancellation."""
    if type(enabled) is not bool:
        raise TypeError('Invalid trusted native tool event flag')
    token = _native_tools.set(enabled)
    try:
        yield
    finally:
        _native_tools.reset(token)


def tool_event_frame(payload: dict, *, result: dict | None = None) -> str | CoreToolEvent:
    """Emit a native observation before SSE encoding, or original Web bytes."""
    native = native_tool_events_enabled()
    observe = _observer.get()
    if not native and observe is None:
        return 'data: ' + json.dumps(payload) + '\n\n'
    observation = result if isinstance(result, dict) else payload
    kind = payload.get('type')
    if kind not in {'tool_start', 'tool_progress', 'tool_output'}:
        raise ValueError('Unknown Core tool event family')
    tool = payload.get('tool')
    safe_tool = tool if type(tool) is str and _NAME.fullmatch(tool) else None
    round_number = payload.get('round')
    safe_round = round_number if type(round_number) is int and round_number >= 0 else None
    if kind == 'tool_start':
        event_type = 'tool.started'
    elif kind == 'tool_progress':
        event_type = 'tool.progress'
    elif isinstance(observation.get('ask_user'), dict) and observation['ask_user'].get('kind') == 'tool_approval':
        event_type = 'tool.awaiting_approval'
    elif observation.get('blocked') or payload.get('execution_attempted') is False:
        event_type = 'tool.blocked'
    elif observation.get('error') or (type(observation.get('exit_code')) is int and observation['exit_code'] != 0):
        event_type = 'tool.failed'
    else:
        event_type = 'tool.completed'
    exit_code = observation.get('exit_code')
    event = CoreToolEvent(
        type=event_type, tool=safe_tool, round=safe_round,
        exit_code=exit_code if type(exit_code) is int else None,
    )
    if observe is not None:
        observe(event)
    return event if native else 'data: ' + json.dumps(payload) + '\n\n'


@contextmanager
def observe_tool_events(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def approval_requested_event(pending) -> CoreApprovalEvent:
    """Construct only from the sealed pending record, never tool-returned data."""
    from src.tool_approvals import PendingToolApproval
    if not isinstance(pending, PendingToolApproval):
        raise TypeError('A sealed pending tool approval is required')
    return CoreApprovalEvent('approval.requested', pending.approval_id)
