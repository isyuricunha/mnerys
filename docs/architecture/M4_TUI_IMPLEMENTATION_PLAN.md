# M4 implementation plan: first-party terminal UI (TUI)

**Status: M4 in progress.** This document is the M4 execution baseline agreed
before implementation. It records the architecture decision, the verified
Gateway surface, the acceptance matrix and the formally recorded submilestones
with measurable exit gates. Related: [Gateway/Agent contract](GATEWAY_AGENT_CONTRACT.md),
[M3 acceptance](M3_ACCEPTANCE.md), [roadmap](../ROADMAP.md).

## Goal and boundaries

Build a keyboard-first, first-party terminal interface that shares the
existing Mnerys Gateway, Agent Core, sessions, events, authentication and
tool permissions with the Web and the CLI. The TUI is a client adapter, not
an authority: it never imports the server application, never touches the
database directly and never executes tools or approvals by itself.

Non-goals: a second agent engine, a parallel session store, full Web feature
parity (M5 owns broader client parity), public PyPI publication, and any
deployment to the private preview as part of M4 (the controlled rollout
procedure is documented for a separately authorized deployment).

## Framework decision: Textual

**Chosen: Textual 8.x (current stable 8.2.8, MIT license, Python >= 3.9,
active monthly releases through 2026).**

Evaluation against the M4 requirements:

| Requirement | Textual 8.x evidence |
| --- | --- |
| Linux/macOS/Windows | Official cross-platform support; Windows Terminal supported |
| Python floor | `requires-python >= 3.9` fits the CLI package floor (>= 3.10) |
| Async event streaming | asyncio-native; `run_worker` background tasks for the event pump |
| Keyboard navigation | First-class bindings, focus model, screen stack |
| Responsive layouts | CSS layout engine with containers, scrollbars, resize handling |
| Rich text and Markdown | Built-in `Markdown` widget and Rich rendering (control sequences render as text, never executed) |
| Accessibility/limits | 24-bit/256-color/limited palettes degrade through Rich; legible fallback styles ship with the app |
| Test automation | `App.run_test(headless, size)` + `Pilot.press/click`; headless 80x24 default |
| Maintenance footprint | Textualize maintains it; MIT; `textual` is the only TUI runtime dependency |
| SSH/tmux | Textual applications are regular full-screen terminal programs; supported |
| Rejected alternatives | `prompt_toolkit` 3.0.53 and `urwid` 5.1.0 are actively maintained but lower-level: layouts, widgets, streaming and test pilots would all be hand-built. They remain reasonable for single-prompt utilities, not a full application |

Handwritten ANSI control sequences are explicitly rejected as fragile.

## Client architecture

The TUI lives in the existing `packages/mnerys-cli` distribution as
`mnerys_cli.tui` (a subpackage), so the unified release version, profile
system and packaging apply unchanged:

- `mnerys` with an interactive TTY launches the TUI; without a TTY it prints
  the CLI help with an explicit note instead of a full-screen application.
- `mnerys tui` launches the TUI explicitly; `--profile NAME` reuses the
  existing named Gateway profile; all other commands are untouched.
- Textual becomes an optional dependency extra (`mnerys-cli[tui]`), keeping
  remote-only installs lightweight; the wheel entry point imports the TUI
  lazily so the base install never requires it.
- One `GatewayClient`-derived async transport per app instance; no competing
  configuration store, no duplicate state that fights Gateway authority.

## Streaming and event consumption

The Gateway v1 event endpoint replays ordered, sanitized typed events after
an `after_seq` cursor and terminates each response with `[DONE]`; the durable
turn record replay behaves identically. The TUI therefore:

1. Creates the turn (`POST /api/v1/turns`) and receives the exact `turn_id`.
2. Polls `GET /api/v1/turns/{turn_id}/events?after_seq=N` from an async
   worker, applying each event exactly once by sequence number.
3. Re-polls until the turn snapshot reports a terminal status
   (completed/failed/cancelled/awaiting_approval), with bounded backoff.
4. On transport failure, reconnects with the last applied cursor; replayed
   events are idempotent (already-applied sequences are skipped, never
   re-rendered).
5. Cancels the exact active turn (`POST /api/v1/turns/{turn_id}/cancel`)
   and never mutates UI state from network threads except through Textual
   workers/messages.

Event families consumed (all server-sanitized already): `message.delta`,
`message.final`, `turn.completed`, `turn.failed`, `turn.cancelled`,
`turn.awaiting_approval`, `tool.started/progress/completed/failed/blocked/awaiting_approval`,
`approval.requested/decided`, `model.route.selected`, `metrics`.

## Verified Gateway v1 surface and required extensions

Existing (verified in `routes/gateway_api.py` and the M3 acceptance):
turn creation, turn snapshot, event replay, exact-turn cancellation,
owner-checked approval decisions, owner-scoped session listing (metadata),
models/agents/tools discovery.

Verified gaps this milestone closes, each narrowly scoped, owner-admitted
through the existing principal/admission policy, revision-fenced where the
session schema supports it:

1. `POST /api/v1/sessions` — owner-scoped session creation. The CLI already
   calls this path; today only a test fixture answers it, so the real router
   must implement it (latent CLI gap found during inspection).
2. `GET /api/v1/sessions/{session_id}` — metadata plus the persisted,
   sanitized conversation history; the same server-owned data the Web
   history route reads, projected for clients (no secrets, no raw tool data).
3. `GET /api/v1/sessions/{session_id}/approvals` (or equivalent narrow
   inspection) — pending approval details for human-readable cards, sourced
   from the durable approval records, never reconstructing authority from
   client-visible cards.
4. Session model preference — expose the server-validated model selection
   for owner sessions when the session schema supports it; the client hint
   is validated against the server model registry before anything is stored.

Foreign sessions remain indistinguishable from missing ones (404), all new
routes require an authenticated owner principal with the `chat` scope, and
no Web application globals leak into the client package.

## Security model

- Messages, Markdown, code blocks, model output, tool results, memory and
  session titles are untrusted data: rendered through Rich/Textual text
  pipelines that do not execute terminal control sequences. A regression
  test asserts ESC-prefixed payloads render inert.
- Approval authority stays server-side: the TUI may display pending
  approvals and submit the owner's decision through
  `POST /api/v1/approvals/{approval_id}/decision`; it cannot approve its own
  turn beyond what the sealed server store permits, cannot decide expired or
  consumed approvals (server rejects), and never fabricates approval cards.
- Credentials never render; tokens live in the existing credential store.
- The TUI never opens database or file-system side channels; every fact it
  shows comes from an authenticated Gateway response.

## Web/TUI continuity requirement

The central acceptance flow: a session started in the Web (or CLI) must be
openable, readable and continuable from the TUI with the same persisted
history, and the reverse direction must hold, without duplicated messages,
terminal events, approvals or tool side effects. Integration tests exercise
this against the real production Gateway router mounted in an isolated
environment (temporary SQLite primary; PostgreSQL 17 parity where the
schema is exercised), not only mocked transports. Concurrent changes ride
the existing session revision/incarnation contracts.

## Acceptance matrix

| Scenario | Required outcome |
| --- | --- |
| `mnerys` in an interactive terminal | TUI launches with the active profile |
| `mnerys` without a TTY | CLI help with an explicit non-interactive note, exit non-zero only where existing semantics require |
| `mnerys tui --profile NAME` | TUI uses that named profile's endpoint/token |
| Streaming turn | Incremental deltas, ordered events, deterministic terminal state |
| Cancel during stream | Exact turn cancelled, UI reaches a clean cancelled state, no orphan workers |
| Network drop mid-stream | Reconnect with cursor, no duplicated visible output, history reconciled |
| Stale events from a previous session/turn | Ignored; UI never renders cross-session data |
| Duplicate terminal events | Applied exactly once |
| Web conversation opened in TUI | Same session, same persisted history |
| Turn sent from TUI, then Web refreshed | The turn appears in the Web session, no duplicate writes |
| Foreign session access | 404, indistinguishable from missing |
| Invalid credentials | Explicit auth error state, exit code 4 semantics preserved |
| Approval requested | Human-readable pending card; approve/deny via the server store only |
| Approval replay/double decision | Server rejects exactly; UI shows the server outcome |
| Malformed/ESC-laden payload | Rendered inert text, UI stable |
| Terminal resize during streaming | Layout reflows; no crash, stream continues |
| 80x24 and narrow terminals | Legible layout, no clipped essential controls |
| Installed wheel with `[tui]` extra | TUI runs independently of any repository checkout |
| Base wheel without the extra | All non-TUI commands unchanged; `tui` prints a clear installation hint |
| Unified versioning | TUI reports the same distribution version as `mnerys --version` |

## Submilestones and exit gates

Formally recorded in [the roadmap](../ROADMAP.md). A submilestone is
complete only when its gate passes with evidence; ordinary commits are not
milestones.

1. **M4.1 Gateway client surface.** v1 endpoints for owner-scoped session
   creation, session metadata + history, approval inspection and the session
   model preference, all admission-checked; async client methods in the CLI
   package. *Gate:* new routes pass negative admission tests against the
   real router (unauthenticated 401, foreign session 404, invalid body 422)
   and the CLI's existing session-creation path works against the real
   router.
2. **M4.2 TUI foundation.** Textual app shell, entry points, profile reuse,
   Eclipse theme, help and shortcut discovery, graceful exit, `[tui]` extra
   packaging. *Gate:* headless `run_test` boots the installed wheel's TUI;
   non-interactive entry prints help; base install stays lightweight
   (packaging regression test).
3. **M4.3 Conversation experience.** History rendering, streaming turn
   consumption, cancellation, reconnection and replay idempotency, multiline
   compose, Markdown/code blocks rendered safely. *Gate:* real streaming
   turn against the router fixture renders incrementally; cancellation and
   replay matrix rows verified; escape-injection test passes.
4. **M4.4 Sessions and discovery.** Session list/create/switch, history
   reload, agent/model selection within server permissions. *Gate:*
   continuity matrix rows pass against the real router in isolation.
5. **M4.5 Tools and approvals.** Tool status rendering, approval cards and
   decisions through the sealed store, denial/expiry/replay handling. *Gate:*
   approval matrix rows pass with server authority intact.
6. **M4.6 Acceptance, packaging and publication.** Full matrix evidence,
   cross-platform CI results (honest about unverified platforms), docs,
   website, release. *Gate:* every M4 exit-gate requirement has evidence;
   release published with version-matched artifacts; website updated.

## Verification and rollout notes

- Tests run against the real production Gateway router in isolated
  environments (temporary data directories, no real user sessions as
  fixtures). A real provider-backed acceptance turn is used only through an
  already-authorized safe path.
- The private preview is NOT redeployed as part of M4. The controlled
  rollout reuses the documented native runner procedure (candidate
  preparation, validation, verified PostgreSQL archive, short controlled
  restart, live verification, automatic rollback on failure) and remains a
  separately authorized step.
- Documentation and the public website update at every validated
  submilestone; the site claims M4 COMPLETE only after the final exit gate.
