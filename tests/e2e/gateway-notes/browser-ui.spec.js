const { test, expect } = require('@playwright/test');

const RUN_ID = 'b'.repeat(32);
const APPROVAL_ID = 'A'.repeat(43);

function approvedActionPayload() {
  return {
    kind: 'tool_approval',
    approval_id: APPROVAL_ID,
    session_id: 'session-1',
    question: 'Allow this task to continue?',
    options: [
      { label: 'Allow for this task', value: 'approve_task' },
      { label: 'Allow for this chat session', value: 'approve' },
      { label: 'Deny', value: 'deny' },
    ],
    action: {
      tool: 'manage_notes',
      content: '{"action":"list","filter":"<img src=x onerror=alert(1)>"}',
      effects: ['READ_EXTERNAL_STATE'],
      digest: 'a'.repeat(16),
    },
  };
}

async function openPendingReview(page) {
  const review = approvedActionPayload();
  const frames = [
    'id: ' + RUN_ID + ':0\ndata: ' +
      JSON.stringify({ type: 'ask_user', data: review }) + '\n\n',
    'id: ' + RUN_ID + ':1\ndata: [DONE]\n\n',
  ].join('');

  let requests = 0;
  await page.route('**/api/chat/resume/session-1', async route => {
    requests++;
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'X-Odysseus-Run-Id': RUN_ID,
        'X-Mnerys-Gateway-Profile': 'notes_v1',
      },
      body: frames,
    });
  });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    const sessions = (await import('/static/js/sessions.js')).default;
    sessions.getCurrentSessionId = () => 'session-1';
    sessions.getSessions = () => [{ id: 'session-1', model: 'fixture-model' }];
    const chat = await import('/static/js/chat.js');
    window.__notesActions = [];
    document.addEventListener('odysseus:tool-approval', event => {
      window.__notesActions.push(event.detail);
    });
    const result = await chat.resumeStream('session-1');
    if (!result) throw new Error('Native notes replay was not accepted');
  });
  expect(requests).toBe(1);
  const card = page.locator('#chat-history .ask-user-card');
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute('data-ask-user-kind', 'tool_approval');
  await expect(card.locator('.ask-user-option')).toHaveCount(3);
  await expect(card).toContainText('manage_notes');
  await expect(card).toContainText(review.action.content);
  await expect(page.locator('#chat-history .msg-ai')).toHaveCount(0);
  await expect(card.locator('img')).toHaveCount(0);
  return card;
}

async function selectedFormData(page) {
  return page.evaluate(async () => {
    const chat = await import('/static/js/chat.js');
    const events = window.__notesActions;
    const selected = events[0];
    if (!selected) return { events, entries: [] };
    const form = chat.buildRestrictedNotesApprovalForm(
      'session-1', selected,
    );
    return { events, entries: [...form.entries()] };
  });
}

test('existing chat module and approval renderer load in a real browser', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message || error)));
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const result = await page.evaluate(async () => {
    const renderer = await import('/static/js/chatRenderer.js');
    const chat = await import('/static/js/chat.js');
    return {
      renderer: typeof renderer.renderAskUserCard,
      resume: typeof chat.resumeStream,
      form: typeof chat.buildRestrictedNotesApprovalForm,
      live: typeof window.chatModule?.resumeStream,
    };
  });
  expect(result).toEqual({
    renderer: 'function', resume: 'function',
    form: 'function', live: 'function',
  });
  expect(errors).toEqual([]);
});

test('desktop notes replay restores a safe interactive card and strict form', async ({ page }) => {
  const card = await openPendingReview(page);
  // Numeric shortcuts must never automatically grant a tool approval.
  await page.keyboard.press('1');
  expect(await page.evaluate(() => window.__notesActions)).toEqual([]);

  await card.locator('.ask-user-option').first().click();
  await expect(card).toHaveClass(/ask-user-answered/);
  const { events, entries } = await selectedFormData(page);
  expect(events).toEqual([{
    approval_id: APPROVAL_ID,
    decision: 'approve_task',
    gateway_profile: 'notes_v1',
  }]);
  expect(entries).toEqual([
    ['gateway_profile', 'notes_v1'],
    ['session', 'session-1'],
    ['mode', 'agent'],
    ['no_memory', 'true'],
    ['use_rag', 'false'],
    ['active_doc_state', 'none'],
    ['tool_approval_id', APPROVAL_ID],
    ['tool_approval_decision', 'approve_task'],
  ]);

  const rejected = await page.evaluate(async () => {
    const chat = await import('/static/js/chat.js');
    const invalid = [
      ['session-1', { gateway_profile: 'notes_v1', approval_id: 'wrong', decision: 'approve' }],
      ['session-1', { gateway_profile: 'text_v1', approval_id: 'A'.repeat(43), decision: 'approve' }],
      ['session-1', { gateway_profile: 'notes_v1', approval_id: 'A'.repeat(43), decision: 'approve_all' }],
      ['', { gateway_profile: 'notes_v1', approval_id: 'A'.repeat(43), decision: 'deny' }],
    ];
    return invalid.map(([session, approval]) => {
      try {
        chat.buildRestrictedNotesApprovalForm(session, approval);
        return false;
      } catch (error) {
        return error instanceof TypeError;
      }
    });
  });
  expect(rejected).toEqual([true, true, true, true]);
});

test('mobile notes review exposes explicit Deny without any tool execution', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { stylesheetUrls } = await import('../../helpers/stylesheets.mjs');
  const card = await openPendingReview(page);
  for (const url of await stylesheetUrls()) {
    await page.addStyleTag({ url });
  }
  await expect(card).toBeVisible();
  const deny = card.locator('.ask-user-option').nth(2);
  await expect(deny).toBeVisible();
  const bounds = await card.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds.x).toBeGreaterThanOrEqual(-1);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(391);
  await deny.click();
  const { events, entries } = await selectedFormData(page);
  expect(events).toEqual([{
    approval_id: APPROVAL_ID,
    decision: 'deny',
    gateway_profile: 'notes_v1',
  }]);
  expect(entries.at(-1)).toEqual(['tool_approval_decision', 'deny']);
  expect(entries).toHaveLength(8);
});


test('read-only post-restart status never becomes a clickable notes approval', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const { stylesheetUrls } = await import('../../helpers/stylesheets.mjs');
  for (const url of await stylesheetUrls()) {
    await page.addStyleTag({ url });
  }
  const result = await page.evaluate(async () => {
    const sessions = await import('/static/js/sessions.js');
    sessions.setCurrentSessionId('session-1');
    const states = [
      sessions.renderNotesRecoveryNotice('session-1', 'interrupted'),
      sessions.renderNotesRecoveryNotice('session-1', 'indeterminate'),
      sessions.renderNotesRecoveryNotice('session-1', 'expired'),
      sessions.renderNotesRecoveryNotice('session-1', '<img src=x onerror=alert(1)>'),
      sessions.renderNotesRecoveryNotice('foreign-session', 'indeterminate'),
    ];
    return states;
  });
  expect(result).toEqual([true, true, true, false, false]);
  const notice = page.locator('#chat-history .notes-recovery-notice');
  await expect(notice).toHaveCount(1);
  await expect(notice).toHaveAttribute('role', 'status');
  await expect(notice).toContainText('expired');
  await expect(notice.locator('button, input, form, .ask-user-option')).toHaveCount(0);
  await expect(page.locator('[data-ask-user-kind="tool_approval"]')).toHaveCount(0);
  const bounds = await notice.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds.x).toBeGreaterThanOrEqual(-1);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(391);
});


test('notes-provenance replay ignores non-notes actionable cards', async ({ page }) => {
  const hostile = approvedActionPayload();
  hostile.action.tool = 'bash';
  hostile.action.content = 'do not execute';
  const frames = 'id: ' + RUN_ID + ':0\ndata: ' +
    JSON.stringify({ type: 'ask_user', data: hostile }) + '\n\n' +
    'id: ' + RUN_ID + ':1\ndata: [DONE]\n\n';
  await page.route('**/api/chat/resume/session-1', route => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    headers: {
      'X-Odysseus-Run-Id': RUN_ID,
      'X-Mnerys-Gateway-Profile': 'notes_v1',
    },
    body: frames,
  }));
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const completed = await page.evaluate(async () => {
    const sessions = (await import('/static/js/sessions.js')).default;
    sessions.getCurrentSessionId = () => 'session-1';
    sessions.getSessions = () => [{ id: 'session-1', model: 'fixture-model' }];
    const chat = await import('/static/js/chat.js');
    return chat.resumeStream('session-1');
  });
  expect(completed).toBe(true);
  await expect(page.locator('.ask-user-card')).toHaveCount(0);
  expect(await page.evaluate(() => document.querySelector('.ask-user-option'))).toBeNull();
});
