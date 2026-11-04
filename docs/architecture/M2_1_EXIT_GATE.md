# M2.1 exit gate — one reusable admitted Agent turn

**Status: PARTIAL — direct in-process Gateway execution is implemented and tested with a synthetic model; complete turn composition and real adapter acceptance remain open.**

This is the bounded, verifiable completion checklist for [M2.1 in the implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md#m21--finish-the-internal-authority-and-turn-boundary), under the [M1 design contract](GATEWAY_AGENT_CONTRACT.md). It replaces open-ended extraction targets with acceptance criteria. An item is complete only when its implementation and negative tests can be identified in the current source.

## Verified now

- **Session identity and admission:** [GatewayAdmission](../../src/gateway_admission.py) validates the server-issued principal against an owner-scoped session lookup; browser, scoped bearer and single-user cases have separate checks. The proof is **in-process**, not an externally usable token.
- **Turn preparation components:** [GatewayTurns](../../src/gateway_turns.py) composes admitted model fallback/context-policy decisions, tool inventory, capability/tool selection, immutable contracts, account/global restrictions, approvals and response jobs. [Context ports](M2_CONTEXT_SERVICE_PORTS.md) reuse legacy memory, attachment and persistence providers without cloning them.
- **Direct Agent execution:** [the admitted execution service](../../src/gateway_agent_execution.py) now binds the final RequestAuthority and ToolPolicy to an issued session **before invoking the existing** [TurnService and Agent Loop](../../src/turn_service.py). The selected model and provider headers come from the loaded session, not runtime overrides. Account/delegated denials can only narrow tool access. A forged/foreign admission, authority or model is rejected. This service imports no HTTP route or FastAPI component.
- **Lifecycle proof:** [isolated execution tests](../../tests/test_mnerys_gateway_agent_execution.py) start a synthetic, no-network turn through **admission → trusted authority → the inherited Agent Loop → direct event delivery**, entirely from Python without the Web router. Completion, early close, provider error and cancellation restore the original bridge and tool-contract context; the outer Gateway facade explicitly closes its inner generator.

These checks prove an internal executable boundary, **not** a fully independent user-facing Gateway or a real-model production run.

## Remaining M2.1 completion gates

1. **One admitted turn composer — OPEN.** Build a single server-owned application entry point that takes an authenticated, repository-loaded session and untrusted turn request, assembles context/policy/authority using the existing trusted ports, and invokes the admitted runtime. Do not allow a remote caller to deserialize a RequestAuthority, policy, principal or bridge. Today, [the Web route](../../routes/chat_routes.py) still orchestrates many of these pieces before it calls Gateway execution.
2. **Transport-neutral turn result and lifecycle — OPEN.** Return typed internal turn events and final results rather than relying on SSE frame strings as the service contract. Preserve exact-run stop/finish/reconnect semantics, existing response persistence/partial saves and approval continuity. Keep SSE serialization in Web. Durable event replay and cross-process transactional fencing belong to later [M2 storage work](SESSION_REVISION_MIGRATION.md); do **not** claim them as part of this in-process gate.
3. **Web compatibility and direct-client parity — OPEN.** Route the existing Web chat path through the single composer, then add no-HTTP full-turn tests and Web-versus-direct parity for browser/bearer admission, tools, Plan/Compare, incognito, attachment/document inputs, approval continuations, model fallback, interruption and error persistence. At least one controlled **real model** turn and full CI are required before accepting the cutover; synthetic model fixtures alone are insufficient.
4. **Evidence and scope audit — OPEN.** Audit the final diff against the [session-writer inventory](SESSION_WRITER_INVENTORY.md) and [M1 contract regression baseline](M1_CONTRACT_TEST_BASELINE.md), validate that no new tool grants, user/session writes, remote entry points or secret-bearing examples slipped in, and update the roadmap. Deployment is a separate approved step with backup and rollback checks.

## Deliberately out of scope for M2.1

- A publicly reachable authenticated Gateway API and independently installable CLI/TUI clients.
- Durable cross-process TurnRecord/TurnEvent storage, revision-fenced writers and replay after server restart.
- Migrating SQLite or PostgreSQL production databases; replacing the original Agent Loop or dispatcher.

Follow the ordered [M2 execution plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) for those milestones. Do not declare M2.1 complete until **all four open gates** have verified evidence.

## Verification for this execution slice

The local regression batches passed **727 focused**, **185 routing/streaming**, **159 authority/approval**, **160 peripheral**, **1,129 contract compatibility** and **148 external schema/tool-edit** tests. The groups overlap; these are not unique test totals. The focused set includes the prior CI failure regressions and synthetic direct Gateway execution. Existing SQLAlchemy and pytest-asyncio deprecation/configuration warnings remain. Hosted CI, live model invocation and preview acceptance have **not** been run for this local change.
