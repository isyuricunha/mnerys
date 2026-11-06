# Browser End-to-End Tests

The photo editor release gate launches an isolated, authentication-disabled
Odysseus server on port `7013` with a temporary SQLite database.

```bash
npm install
npm run test:photo-editor:install
npm run test:photo-editor
```

Set `PHOTO_EDITOR_E2E_PORT` to use another port. Failure screenshots and traces
are written to `test-results/photo-editor/`. The server uses the repository
`.venv` automatically; set `ODYSSEUS_TEST_PYTHON` to another Python executable
when using a different environment layout.

## Gateway native notes UI contract

```bash
npm run test:gateway-notes
```

The `tests/e2e/gateway-notes/` suite runs the existing browser-side
`chat.js`, `sessions.js` and `chatRenderer.js` in installed Chromium,
with mocked, sealed-format HTTP/SSE fixtures. It checks that replay of an
existing `notes_v1` review shows a readable actionable card on desktop/mobile
without creating a phantom assistant response, rejects a non-notes card,
does not approve via keyboard shortcuts, and produces only the eight fields
allowed in the strictly admitted approval form.

Unlike the photo-editor suite, the notes fixture uses a **loopback-only static
server** that serves only `static/` assets. It does not launch `app:app`,
initialize real MCPs, memory/vector services, provider connections, jobs or
database clients. `MNERYS_NOTES_E2E_PORT` (default `7014`) selects the
test-only listener. The test asserts the real browser renderer and form
serializer, **not** a running real-model/tool/backend workflow. Continue to
run the Python route/integration tests and the isolated SQLite/PostgreSQL
matrix for server behavior. No environment secrets or production data are
required for this browser contract.

## Gateway browser-to-HTTP integration (isolated)

```bash
MNERYS_NOTES_E2E_PYTHON=.venv/bin/python npm run test:gateway-notes-http
```

This second Playwright suite mounts the original Gateway HTTP handler,
approval store, typed SSE and original persisted writer behind a **test-only
ASGI application**; it deliberately does not import or launch the production
`app:app` initializer. Authentication is replaced with a synthetic test
admission for a single in-memory session, and the provider/model and tool
effects are synthetic. The harness fails to start unless its dedicated
test-only environment flag is set; it binds loopback only and receives
a minimized environment **without provider credentials or database URLs**.
The requested port defaults to `7015` and can be changed with
`MNERYS_NOTES_HTTP_E2E_PORT`.

The suite verifies both the actual browser review card to HTTP Gateway
continuation and the original `handleChatSubmit` running with markup
from `static/index.html`, including the case where the normal composer
button is disabled. The result must have one exact-action claim, one user
receipt, one assistant receipt and one inherited post-response callback;
a replayed click is denied. A third browser-to-route case simulates loss of
the original run while retaining its private SQL decision ledger: the
authenticated status response contains only `review_unavailable` metadata,
the UI shows a non-actionable notice, and neither tools nor model are run.
The restricted `/api/chat/notes_journal/{session_id}` endpoint also
replays only persisted, owner-scoped lifecycle milestones. The existing
Web session recovery notice now displays a short, allowlisted, inert
summary of those milestones, without a cursor, approval ID or tool input.
The browser tests cover original-run loss, malicious/unrecognized event
kinds, and journal unavailability while keeping the warning visible.
No approval IDs or tool content are returned, even when the original
in-memory run has been discarded. The test-only reset
clears **only** the fixture's disposable in-memory SQL tables because
the synthetic message IDs intentionally repeat; a non-disposable
database is refused.

Browser fixtures are reset between tests and never call external services.
Failure traces and screenshots are retained
under `~/chatgpt/mnerys/tests/gateway-notes-http/`, not the Git checkout.

**Limit:** test-authenticated routes, synthetic models and in-memory
writers do not establish full production authentication, real provider
parity, durable signed-approval/stream recovery or multi-worker safety.
Separately, the Python
[restart regression](../test_mnerys_gateway_notes_restart_process.py)
reopens disposable SQLite and PostgreSQL 17 from independent interpreters,
confirming only metadata status/replay and refusal to reserve an action twice.
