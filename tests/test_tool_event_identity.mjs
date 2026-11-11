import test from 'node:test';
import assert from 'node:assert/strict';
import { toolEventCallId, toolNodeForEvent } from '../static/js/toolEventIdentity.mjs';

test('results and progress are attached only to their matching native call IDs', () => {
  const first = { name: 'pwd' };
  const second = { name: 'git status' };
  const map = new Map([['call-a', first], ['call-b', second]]);
  assert.equal(toolEventCallId({ call_id: 'call-a' }), 'call-a');
  assert.equal(toolNodeForEvent({ call_id: 'call-a' }, map, second), first);
  assert.equal(toolNodeForEvent({ tool_call_id: 'call-b' }, map, first), second);
  assert.equal(toolNodeForEvent({ call_id: 'unknown' }, map, second), null);
  assert.equal(toolNodeForEvent({ call_id: 'unknown' }, new Map(), first), null);
});

test('old events without call IDs retain one-action compatibility', () => {
  const fallback = { name: 'approved shell action' };
  assert.equal(toolNodeForEvent({}, new Map(), fallback), fallback);
  assert.equal(toolNodeForEvent({ call_id: '' }, new Map(), fallback), fallback);
  assert.equal(toolNodeForEvent({}, new Map()), null);
});
