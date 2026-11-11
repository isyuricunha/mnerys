# M5.3 — Agent-First Experience Contract (Increment 5)

Agent is the single, natural default experience across Web, CLI and TUI. The
visible Chat/Agent mode switch is removed; every new interactive turn runs the
ONE agent-first execution path through the existing Agent Core and Gateway.
This is a controlled compatibility migration — not a new engine.

## The unified behavior

- **One engine**: `stream_agent_loop` remains the sole interactive execution
  path. The legacy plain-chat adapter (`stream_llm` directly, no tools, no
  document access) is retained ONLY as the narrowly scoped compatibility
  ceiling for callers whose privileges demote them (`can_use_agent=false`) —
  documented below.
- **A conversation that does not need tools still works naturally**: with no
  tool offered (or a model without tool support), the agent loop streams an
  ordinary answer — tools are never forced into a response, and no tool call
  is fabricated.
- **Tool availability is authorization, not a label**: the offered surface
  comes from the canonical registry, tool policy, request privileges
  (`can_use_agent`), request-authority grants and session context — the same
  for every client. An Agent label grants nothing; a Chat label grants
  nothing.

## Legacy compatibility boundary

- The `mode` wire value is accepted unchanged (`chat`, `agent`, empty,
  unknown). `resolve_unified_chat_mode` (src/gateway_execution_policy.py) is
  the ONE normalization point: an agent-permitted caller runs the agent-first
  path; a demoted caller is forced to `chat` (the admitted policy's demotion
  precedes Compare's chat-only clamp, matching the legacy order).
- The explicitly opted-in strict text-profile route (the `gateway_profile=text_v1`
  cutover, `MNERYS_M21_WEB_TEXT_ROUTING=1`) legitimately requires a RAW
  plain-chat declaration and is evaluated against the un-normalized value —
  the compatibility route keeps its no-tools/no-side-context guarantees.
- Historical `mode` values in the sessions table and transcripts remain
  readable and resumable with NO destructive migration; the session list/
  detail payloads expose the effective state.
- The `turn_mode` SSE event remains emitted (now stamped `agent` for unified
  turns); historical bubbles keep their recorded mode pills.
- Never reinterpret legacy Chat mode as a permission grant, and never upgrade
  an unauthorized caller into a tool-capable principal.

## Web unification (what changed, what stayed)

- **Removed**: the composer's `#mode-agent-btn`/`#mode-chat-btn` toggle; the
  tool-error auto-switch that flipped modes and rewrote the error text; the
  set_mode event's button toggling and `[data-mode-tool]` hiding (tool
  toggles are always visible — they are per-turn opt-ins, not mode features).
- **Kept**: session selection/creation/restoration, streaming + thinking
  indicators, message history, tool events, approval cards (all four
  decisions + the task scope), the YOLO indicator, workspace/CWD selection,
  model/preset preferences, attachments/files/images, markdown/code
  rendering, cancellation/reconnection, keyboard shortcuts, accessibility.
  The auto-escalation intents (documents open, workspace targets) are no
  longer needed client-side — the server owns routing.
- The `.mode-toggle` CSS stays (the Cookbook/Serve panels legitimately reuse
  it).

## Cross-client continuity

CLI/TUI create sessions through the same Gateway v1 API and run the same
agent-first loop; sessions are client-agnostic. CWD, YOLO state, approvals,
denied tools, and saved rules are server-owned and identical for every
authorized client. Historical chat-created and agent-created sessions both
open and resume normally.

## Security invariants (verified by focused tests)

1. `mode=agent` never grants tools to an unauthorized caller (the demotion
   ceiling is authorization-driven).
2. Legacy mode values never bypass policy; explicit denials survive.
3. YOLO stays opt-in; CWD stays authoritative; read-before-edit and artifact
   publication stay enforced.
4. Cross-owner isolation, approval scoping, replay protection and MCP names
   are unchanged.
5. Model-generated text still cannot alter authorization, mode state, or the
   tool surface.

## Test evidence (increment 5)

`tests/test_agent_first_unification.py` — 12 deterministic tests: the mode
normalization matrix (legacy chat/empty/unknown → agent; demoted callers →
the chat ceiling), the final-policy demotion, the demoted caller's
tools=None execution adapter (audited in the route source), policy denials
surviving unification, the composer HTML/JS no longer driving the toggle,
the shared `.mode-toggle` CSS surviving for other panels, historical mode
pills staying readable, and the `turn_mode` event contract. Plus: the
strict-cutover suites (71), the final-policy/chat-route policy suites, the
TUI/Gateway execution suites, and the frontend Node architecture tests.

## Remaining M5.3 work

The final hosted acceptance (risky-command prompts across all surfaces, the
four decisions end-to-end, YOLO/deny/owner-boundary negatives).
