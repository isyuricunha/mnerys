const { test, expect } = require('@playwright/test');

test.beforeEach(async ({ request }) => {
  const cleared = await request.post('/__fixture/reset');
  expect(cleared.ok(), await cleared.text()).toBe(true);
});

const BASE_FORM = {
  gateway_profile: 'notes_v1',
  session: 'session-1',
  mode: 'agent',
  no_memory: 'true',
  use_rag: 'false',
  active_doc_state: 'none',
};

test('real browser action -> guarded HTTP route -> original sealed Gateway approval once', async ({ page, request }) => {
  const result = await request.post('/api/chat_stream', {
    multipart: { ...BASE_FORM, message: 'List my notes' },
  });
  expect(result.ok(), await result.text()).toBe(true);
  const responseText = await result.text();
  expect(responseText).toContain('"type":"ask_user"');
  const pendingCard = responseText.split('\n')
    .filter(line => line.startsWith('data: '))
    .map(line => line.slice(6))
    .filter(line => line !== '[DONE]')
    .map(line => JSON.parse(line))
    .find(frame => frame.type === 'ask_user')?.data;
  expect(pendingCard?.approval_id).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(responseText).not.toContain('FIXTURE_TOOL_PRIVATE');
  expect(result.headers()['x-mnerys-gateway-profile']).toBe('notes_v1');

  const initialJournal = await (await request.get('/api/chat/notes_journal/session-1')).json();
  expect(initialJournal.events.map(item => item.kind)).toEqual(['review_registered']);
  expect(JSON.stringify(initialJournal)).not.toContain(pendingCard.approval_id);
  const before = await (await request.get('/__fixture/state')).json();
  expect(before).toMatchObject({
    proposals: 1, pending: 1, effects: 0,
    user_messages: 1, assistant_messages: 0,
    post_response_jobs: 0,
  });

  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const jsReady = await page.evaluate(async () => {
    const sessions = (await import('/static/js/sessions.js')).default;
    sessions.getCurrentSessionId = () => 'session-1';
    sessions.getSessions = () => [{ id: 'session-1', model: 'fixture-model' }];
    const chat = await import('/static/js/chat.js');
    window.__approvalHttpResponses = [];
    document.addEventListener('odysseus:tool-approval', async event => {
      const form = chat.buildRestrictedNotesApprovalForm('session-1', event.detail);
      try {
        const response = await fetch('/api/chat_stream', { method: 'POST', body: form });
        window.__approvalHttpResponses.push({
          status: response.status,
          body: await response.text(),
        });
      } catch (error) {
        window.__approvalHttpResponses.push({ error: String(error) });
      }
    });
    return chat.resumeStream('session-1');
  });
  expect(jsReady).toBe(true);

  const card = page.locator('#chat-history .ask-user-card');
  await expect(card).toBeVisible();
  await card.locator('.ask-user-option').first().click();
  await expect.poll(async () => page.evaluate(() => window.__approvalHttpResponses.length)).toBe(1);
  const approved = await page.evaluate(() => window.__approvalHttpResponses[0]);
  expect(approved.status).toBe(200);
  expect(approved.body).toContain('Browser Gateway integration complete.');
  expect(approved.body).toContain('"type":"message_saved"');
  expect(approved.body).not.toContain('FIXTURE_TOOL_PRIVATE');

  const completedJournal = await (await request.get('/api/chat/notes_journal/session-1')).json();
  expect(completedJournal.events.map(item => item.kind)).toEqual([
    'review_registered', 'decision_reserved', 'assistant_committed',
  ]);
  const after = await (await request.get('/__fixture/state')).json();
  expect(after).toMatchObject({
    proposals: 1, pending: 0, effects: 1,
    writes: 1, user_messages: 1,
    assistant_messages: 1, post_response_jobs: 1,
    human_checks: 1,
  });

  const repeat = await request.post('/api/chat_stream', {
    multipart: {
      ...BASE_FORM,
      tool_approval_id: pendingCard.approval_id,
      tool_approval_decision: 'approve_task',
    },
  });
  expect([403, 409]).toContain(repeat.status());
  const final = await (await request.get('/__fixture/state')).json();
  expect(final.effects).toBe(1);
  expect(final.writes).toBe(1);
});


test('lost original run returns only safe SQL recovery status after browser reconnect', async ({ page, request }) => {
  const started = await request.post('/api/chat_stream', {
    multipart: { ...BASE_FORM, message: 'List my notes' },
  });
  expect(started.ok(), await started.text()).toBe(true);
  expect(await started.text()).toContain('"type":"ask_user"');
  const forgotten = await request.post('/__fixture/forget-run');
  expect(forgotten.ok(), await forgotten.text()).toBe(true);

  const statusResponse = await request.get('/api/chat/stream_status/session-1');
  expect(statusResponse.ok()).toBe(true);
  const status = await statusResponse.json();
  expect(status).toEqual({
    status: 'review_unavailable',
    gateway_profile: 'notes_v1',
    recovery: 'interrupted',
    detached: false,
  });
  const serializedStatus = JSON.stringify(status);
  const durableEvents = await (await request.get('/api/chat/notes_journal/session-1')).json();
  expect(durableEvents.events.map(item => item.kind)).toEqual(['review_registered']);
  expect(JSON.stringify(durableEvents)).not.toContain('approval_id');
  expect(serializedStatus).not.toContain('approval_id');
  expect(serializedStatus).not.toContain('action');
  expect(serializedStatus).not.toContain('manage_notes');

  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const rendered = await page.evaluate(async () => {
    const sessions = await import('/static/js/sessions.js');
    sessions.setCurrentSessionId('session-1');
    const fetched = await (await fetch('/api/chat/stream_status/session-1')).json();
    return sessions.renderNotesRecoveryWithJournal('session-1', fetched.recovery);
  });
  expect(rendered).toBe(true);
  const notice = page.locator('.notes-recovery-notice');
  await expect(notice).toBeVisible();
  await expect(notice).toContainText('no longer available');
  await expect(notice.locator('.notes-recovery-milestones')).toContainText('Review registered');
  await expect(notice.locator('.notes-recovery-milestones')).toContainText('cannot approve or retry');
  await expect(notice).not.toContainText(durableEvents.events[0].cursor);
  await expect(notice.locator('button, input, form, .ask-user-option')).toHaveCount(0);
  await expect(page.locator('[data-ask-user-kind="tool_approval"]')).toHaveCount(0);

  // Even a compromised/malformed response can render only known, inert labels.
  const journalUrl = url => url.pathname === '/api/chat/notes_journal/session-1'
    && url.searchParams.get('limit') === '50';
  await page.route(journalUrl, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      gateway_profile: 'notes_v1',
      events: [
        { kind: '<img src=x onerror=alert(1)>', cursor: 'PRIVATE_CURSOR' },
        { kind: 'decision_reserved', cursor: 'OTHER_PRIVATE_CURSOR' },
      ],
    }),
  }));
  expect(await page.evaluate(async () => {
    const sessions = await import('/static/js/sessions.js');
    return sessions.renderNotesRecoveryWithJournal('session-1', 'interrupted');
  })).toBe(true);
  await expect(notice.locator('.notes-recovery-milestones')).toContainText('execution not confirmed');
  await expect(notice).not.toContainText('PRIVATE_CURSOR');
  await expect(notice).not.toContainText('onerror');
  await expect(notice.locator('img, button, input, form')).toHaveCount(0);

  // The original safety notice remains when the optional journal is offline.
  await page.unroute(journalUrl);
  await page.route(journalUrl, route => route.fulfill({ status: 503, body: 'unavailable' }));
  expect(await page.evaluate(async () => {
    const sessions = await import('/static/js/sessions.js');
    return sessions.renderNotesRecoveryWithJournal('session-1', 'interrupted');
  })).toBe(true);
  await expect(notice).toContainText('no longer available');
  await expect(notice.locator('.notes-recovery-milestones')).toHaveCount(0);
  await expect(notice.locator('button, input, form')).toHaveCount(0);

  const replay = await request.get('/api/chat/resume/session-1');
  expect(replay.status()).toBe(404);
  const counts = await (await request.get('/__fixture/state')).json();
  expect(counts).toMatchObject({
    proposals: 1, pending: 1, effects: 0,
    assistant_messages: 0, post_response_jobs: 0,
  });
});


test('actual application markup and chat submit handler dispatch notes review', async ({ page, request }) => {
  const observedPosts = [];
  page.on('response', response => {
    if (response.request().method() === 'POST' && response.url().endsWith('/api/chat_stream')) {
      observedPosts.push(response.status());
    }
  });
  const before = await (await request.get('/__fixture/state')).json();
  const first = await request.post('/api/chat_stream', {
    multipart: { ...BASE_FORM, message: 'List my notes' },
  });
  expect(first.ok(), await first.text()).toBe(true);
  expect(await first.text()).toContain('"type":"ask_user"');

  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  const initialized = await page.evaluate(async () => {
    // Load the existing production HTML, but deliberately remove all scripts
    // before injection: this integration test has no app initialization.
    const rawHtml = await (await fetch('/static/index.html')).text();
    const parsed = new DOMParser().parseFromString(rawHtml, 'text/html');
    parsed.querySelectorAll('script').forEach(node => node.remove());
    document.body.innerHTML = parsed.body.innerHTML;
    // The real app bootstrap removes this splash. We skip that bootstrap
    // deliberately, so clear only the fixture's leftover visual overlay.
    document.getElementById('app-loader')?.remove();
    const sessions = (await import('/static/js/sessions.js')).default;
    sessions.getCurrentSessionId = () => 'session-1';
    sessions.getSessions = () => [{ id: 'session-1', model: 'fixture-model' }];
    const chat = await import('/static/js/chat.js');
    const sendButton = document.querySelector('.send-btn');
    if (!sendButton) throw new Error('Production send button is unavailable');
    // Reproduce the idle/composer-disabled state explicitly. The approval
    // must still be submitted as a control-plane action.
    sendButton.disabled = true;
    window.__sendFailures = [];
    window.__sendAttempts = 0;
    window.__approvalEvents = [];
    document.addEventListener('odysseus:tool-approval', event => {
      window.__approvalEvents.push(event.detail);
    });
    sendButton.addEventListener('click', () => {
      window.__sendAttempts++;
      chat.handleChatSubmit({ preventDefault() {} }).catch(err => {
        window.__sendFailures.push(String(err));
      });
    });
    await chat.resumeStream('session-1');
    return {
      sendButtons: document.querySelectorAll('.send-btn').length,
      disabledComposerButton: sendButton.disabled,
      cards: document.querySelectorAll('#chat-history .ask-user-card').length,
    };
  });
  expect(initialized.cards).toBe(1);
  expect(initialized.sendButtons).toBeGreaterThan(0);
  expect(initialized.disabledComposerButton).toBe(true);
  const card = page.locator('#chat-history .ask-user-card');
  await expect(card).toBeVisible();
  await card.locator('.ask-user-option').first().click();

  await expect.poll(async () => {
    const current = await (await request.get('/__fixture/state')).json();
    return current.effects - before.effects;
  }, { timeout: 10_000 }).toBe(1);
  expect(await page.evaluate(() => window.__sendFailures)).toEqual([]);
  // The original notes-specific handler must not depend on click() of a
  // disabled composer button. The only actual POST comes from chat.js.
  expect(await page.evaluate(() => window.__sendAttempts)).toBe(0);
  const observedApproval = await page.evaluate(() => window.__approvalEvents);
  expect(observedApproval).toHaveLength(1);
  expect(observedApproval[0]).toMatchObject({
    gateway_profile: 'notes_v1',
    decision: 'approve_task',
  });
  expect(observedPosts).toEqual([200]);
  const after = await (await request.get('/__fixture/state')).json();
  expect(after.writes - before.writes).toBe(1);
  expect(after.post_response_jobs - before.post_response_jobs).toBe(1);
  expect(after.user_messages - before.user_messages).toBe(1);
});
