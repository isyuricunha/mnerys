// Correlate native tool events by the exact call ID returned by the Gateway.
// A missing/unknown ID must never update an unrelated running tool card.
export function toolEventCallId(event) {
  const value = event?.call_id || event?.tool_call_id;
  return typeof value === 'string' ? value.trim() : '';
}

export function toolNodeForEvent(event, nodesById, legacyCurrentNode = null) {
  const id = toolEventCallId(event);
  // Old events and single-action approval continuations may have no call ID.
  // That compatibility fallback is forbidden when a concrete ID is present.
  return id ? (nodesById.get(id) || null) : (legacyCurrentNode || null);
}
