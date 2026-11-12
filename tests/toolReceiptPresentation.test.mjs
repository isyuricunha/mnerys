import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toolReceiptStatus, toolOutputHeading } from '../static/js/toolReceiptPresentation.mjs';

test('actual exit status is visible for each executed tool', () => {
  assert.equal(toolReceiptStatus({ exit_code: 0 }), 'exit 0');
  assert.equal(toolReceiptStatus({ exit_code: 128 }), 'exit 128');
  assert.equal(toolReceiptStatus({ exit_code: 1 }), 'exit 1');
  assert.equal(toolReceiptStatus({}), 'done');
  assert.equal(toolReceiptStatus({ exit_code: null }), 'done');
  assert.equal(toolReceiptStatus({ exit_code: 128 }, 'awaiting approval'), 'awaiting approval');
});

test('long output is labeled and stays available for explicit expansion', () => {
  assert.equal(toolOutputHeading(''), 'Output');
  assert.equal(toolOutputHeading('/workspace'), 'Output');
  assert.equal(toolOutputHeading('# README\ntext\ncode\n'), 'Output · 3 lines');
  assert.equal(toolOutputHeading('line1\n\nline3'), 'Output · 3 lines');
});

test('tool outputs are treated as data, not interpolated as HTML', () => {
  assert.equal(toolOutputHeading('<script>alert(1)</script>\nline2'), 'Output · 2 lines');
  assert.equal(toolReceiptStatus({ exit_code: '0' }), 'done');
});
