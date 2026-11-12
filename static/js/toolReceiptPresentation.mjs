// Shared, provider-neutral labels for live tool results and history replay.
// All text is derived from the actual gateway receipt, never the model's prose.
export function toolReceiptStatus(event, approvalStatus = '') {
  if (approvalStatus) return String(approvalStatus);
  const exit = event?.exit_code;
  return Number.isInteger(exit) ? `exit ${exit}` : 'done';
}

export function toolOutputHeading(output) {
  const text = String(output ?? '');
  if (!text.trim()) return 'Output';
  const lines = text.replace(/\n$/, '').split('\n').length;
  return lines > 1 ? `Output · ${lines} lines` : 'Output';
}
