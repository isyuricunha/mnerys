# M4 acceptance: first-party terminal UI (TUI)

**Status: COMPLETE (November 6, 2026).** All M4 submilestones passed
their gates; the full CI matrix is green; release v0.15.1 was published
with version-matched wheel and sdist artifacts, the tag is synchronized
on GitHub, GitLab and Forgejo, and the installed CLI reports the release
version with the TUI booting from the installed wheel. Everything in this
matrix is backed by a real test or a real command result.

Scope reminder: the TUI shares the existing Gateway, Agent Core, sessions,
events, authentication and tool permissions with the Web and the CLI. It is
a client adapter; there is no second agent engine, no TUI-owned session
store and no local approval authority.

## Framework decision

**Textual 8.x (8.2.8 at implementation time).** Evaluated against
prompt_toolkit 3.0.53 and urwid 5.1.0 with the official documentation and
current stable versions: Textual offers the only first-class combination of
asyncio streaming workers, keyboard-first widget framework, built-in
Markdown rendering, headless `App.run_test` + Pilot testing with explicit
terminal sizes, and an MIT license with active monthly releases. The TUI is
the optional `mnerys-cli[tui]` extra; the base wheel stays remote-only
lightweight (httpx only).

## Submilestone evidence

### M4.1 Gateway client surface (complete)

| Gate | Evidence |
| --- | --- |
| Owner-scoped session creation from registered endpoints | `tests/test_mnerys_gateway_sessions_v1.py`: default and explicit endpoint resolution, model hint validation, unknown endpoint/model 400, absent endpoint fails closed |
| Owner-scoped session detail and persisted history | same suite: sanitized history page (hidden rows excluded, inline media stripped, pagination flags) |
| Durable approval projections and pending-approval preview | same suite: display-only projection (digests and authority never leave), owner-checked peek, 503 without the store |
| Session preference update (name/model), server-validated | same suite: rename persists, model switch validated against the registry, foreign session 404 |
| CLI's session-creation path against the real router | `tests/test_mnerys_cli_live_sessions.py`: installed binary creates and reuses sessions over real HTTP with an empty fixture |
| Async client for the TUI | `tests/test_mnerys_cli_async_client.py`: incremental SSE parsing, auth mapping, transport failures |

### M4.2 TUI foundation (complete)

| Gate | Evidence |
| --- | --- |
| `mnerys tui [--profile]` and interactive bare `mnerys` | `tests/test_mnerys_tui_shell.py`: profile resolution, config-error exit 3, non-TTY help |
| Headless boot of the installed wheel's TUI | `tests/test_mnerys_cli_packaging.py::test_tui_boots_from_the_installed_wheel` |
| Optional extra stays optional | packaging suite: textual only behind `extra == 'tui'`; base install answers `tui` with the hint and exit 3 |
| Eclipse theme, help, graceful quit | shell suite: help opens/closes, quit exits, error states render |

### M4.3 Conversation experience (complete)

| Gate | Evidence |
| --- | --- |
| Persisted history renders from the Gateway | `tests/test_mnerys_tui_chat.py` (history with literal markup and ESC payloads) and the continuity suite |
| Streaming turns, ordered, idempotent | `tests/test_mnerys_tui_chat.py` (tracker unit tests): exactly-once application, final-wins, terminal states |
| Cancellation of the exact turn | chat suite + router integration: escape cancels over the real router; quitting cancels first |
| Reconnection with cursor, bounded give-up | pump unit tests: transport failure recovers with the same cursor, no duplicates; five consecutive failures raise |
| Escape injection inert | chat suite: `[green]fake markup[/]` and `\x1b[31m` render as literal text |
| Multiline composition | chat suite: Alt+Enter newline, Enter sends |

### M4.4 Sessions and discovery (complete)

| Gate | Evidence |
| --- | --- |
| Session list, create, switch with history reload | `tests/test_mnerys_tui_sessions.py`: list renders, open swaps the conversation, create goes through the Gateway |
| Model selection server-validated | same suite: PATCH sent with the picked model, header updates; the picker shows only the Gateway registry |
| Current session preserved across navigation | chat suite header and session swap tests |

### M4.5 Tools and approvals (complete)

| Gate | Evidence |
| --- | --- |
| Tool status lines from typed events | `tests/test_mnerys_tui_approvals.py`: started/completed/failed/blocked render human-readable and safe |
| Human-readable pending approval card | same suite: preview payload question rendered; display only |
| Approve/deny through the sealed store | same suite: `a`/`d` send the decision; the server's consumed/denied outcome renders |
| Replayed or expired decisions show the server rejection | same suite: 409 renders "rejected by the Gateway" |
| Invalid credentials | same suite: 401 renders the auth error |

### M4.6 Acceptance and publication (complete)

- Web/TUI continuity: `tests/test_mnerys_tui_continuity.py` runs the real
  production router with a real SQLite database and durable records: a
  Web-written conversation renders in the TUI, a TUI turn finalizes in the
  durable store with ordered event replay, and Web reads show no duplicated
  messages. The reverse direction is covered by the same shared store: the
  Web adapter reads what the TUI wrote.
- Independent review: three review rounds over the M4 diff cleared every
  substantive finding (router boundary, authentication masking in the test
  suite, terminal control-character and markup injection, double-submit
  race, tag-time packaging test); the final sweep returned APROVADO.
- Packaging, versions and release follow the unified policy (release
  workflow builds/verifies/attaches the wheel and sdist).
- Cross-platform: Linux is verified in CI and locally; the Windows/macOS
  branches for entry points, config paths and signals are coded but not
  yet verified on real runners. This is an honest limitation, not a claim.

## Security stance (unchanged from the plan)

- Ownership and admission: every new v1 endpoint requires an authenticated
  owner; foreign sessions are indistinguishable from missing (404).
- Untrusted text: messages, Markdown, tool payloads and session titles
  render through markup-disabled or Rich/Markdown pipelines; ESC and markup
  injection are covered by regression tests.
- Approvals: the TUI may display and decide; it never fabricates authority,
  and only the server's sealed, singly-consumable store outcome is shown.
- The router boundary test (`test_direct_loop_bypass_is_not_possible`)
  still rejects database or agent-loop access from the router surface; the
  session storage lives in server-owned services.

## Deployment notes for the subsequent controlled rollout

The M4 code is NOT deployed to the private preview as part of this
milestone. When authorized, the rollout must follow the documented native
runner procedure (`scripts/deploy_native_preview.py`): candidate
preparation from a clean main checkout, validation, a verified PostgreSQL
17 archive before promotion, short controlled restart, live verification,
and automatic rollback on failure. The v1 session endpoints and the TUI
require no schema change beyond the already-deployed M2 migrations; the
new endpoints reuse the existing session, endpoint and durable-record
tables.
## Private preview operational rollout (v0.15.2 -> v0.15.3)

Deployed through the versioned native promotion runner on the private
preview: `1cdd61c6d375` (pre-M4) -> `7eb313d4dbe4` (v0.15.2) -> `v0.15.3`
(necessary validated successor). Evidence recorded in docs/deployment/
NATIVE_PREVIEW.md; the following operational categories were verified
against the LIVE private Gateway, not fixtures:

- Backup and recovery: fresh PostgreSQL 17 archive per promotion, verified
  via pg_restore and an isolated restore into a disposable pg17 instance
  with exact parity (22 sessions / 97 chat messages / durable records /
  approvals all matching the live counts). Rollback procedure documented and
  the previous release preserved.
- Gateway contracts on the live service: owner-scoped session create/list/
  detail with sanitized persisted history, server-resolved model selection
  and PATCH round-trip, approvals surfaces, durable turn records with
  ordered event replay, exact-turn cancellation (turn.cancelled), foreign
  session 404s and invalid credentials 401s.
- Real provider-backed turns: minimax-m3.1-flash on the integrated NVIDIA
  endpoint completed with thinking stream, final message and token metrics
  (turn.completed). Per-model rows without a pasted key fail closed with
  provider_error 401, which is expected endpoint configuration, not a
  client defect.
- Web/CLI/TUI continuity on one authorized session: a CLI turn appeared in
  the TUI; a TUI turn appeared in the v1 detail and in the Web history
  reader; the Web UI (admin session) rendered the same conversation rows
  created by the CLI and the TUI. The Web reader and the v1 detail read the
  same ordered store rows.
- Live terminal-security probes: a stored user message carrying SGR and
  OSC 52 payloads plus fake rich markup rendered inert in a real PTY TUI
  (zero ESC bytes, literal brackets), through the installed wheel.
- Found and fixed during the rollout (root causes, with regression tests):
  the v1 turn route double-persisted the assistant reply (outer wrap over
  the composer's own persistence) writing two chat_messages rows per turn;
  an aborted SSE connection (server restart mid-turn) crashed the TUI to
  its error screen via httpx ResponseNotRead; the composer did not hold
  keyboard focus on open; quit was unreachable while composing (ctrl+q
  added). Each fix has coverage in the client, pump and pilot suites.
- Honest limitations: Windows/macOS terminals remain unverified (no real
  platform evidence); signup is disabled on the private preview so the
  disposable identity could not log into the Web interactively (the Web UI
  proof used the admin identity, which owns all preview sessions); a
  transient typed-run AttributeError occurred once during the live probes
  and did not reproduce on identical subsequent turns; pre-existing
  duplicate assistant rows (resume-time writes, 2026-11-04/06) remain in
  old session history and are not part of this milestone's data.
