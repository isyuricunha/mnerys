# Architecture Documentation

The [Mnerys roadmap](../ROADMAP.md) defines the **target** Python Agent Core, authoritative Gateway and shared CLI/TUI/Web interfaces. The [M1 exit review](M1_EXIT_REVIEW.md) closes the **audit-and-contract-design scope only** and preserves all operational Gateway, multi-transport and durability gates for the [M2 implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md). The M1 audit documents inherited behavior and **proposes** a shared contract. Opt-in process-local SSE replay cursors, the first internal TurnService extraction, typed Gateway session admission, a read-only session ownership lookup port, account privilege-denial preparation and shared message persistence serialization are implemented. Experimental incarnation-fenced background delivery and cache reconciliation are tested **without production activation**. The inherited session append/cache failure path and agent metadata tool double-write issue have targeted compatibility fixes. The first [M2.1 authority extraction](M2_GATEWAY_IMPLEMENTATION_PLAN.md#m21--finish-the-internal-authority-and-turn-boundary) now uses `GatewayTurns.build_authority` with a verified admission proof to construct the Web stream's initial request authority, without independent owner/session inputs and with delegated-credential denial retained. These seams reuse the inherited agent loop and run manager; a standalone authenticated Gateway, durable events and Mnerys CLI/TUI are not implemented yet.

- [M1 design exit review](M1_EXIT_REVIEW.md) — completed design deliverables, verified scope and explicit operational limits.
- [M2 Gateway implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) — prioritized Core/Gateway extraction, durable-state migration, real transport acceptance, rollback gates.
- [M1 audit of the inherited runtime](AGENT_CORE_GATEWAY_M1_AUDIT.md) — verified source map, existing security seams and extraction order.
- [Proposed Gateway / Agent Core contract](GATEWAY_AGENT_CONTRACT.md) — draft entities, responsibilities, events, approvals and compatibility gates.
- [M1 contract regression baseline](M1_CONTRACT_TEST_BASELINE.md) — Web owner/scope/cancellation characterization and shared admission regressions, plus remaining integration gaps.
- [Session revisions and concurrency migration](SESSION_REVISION_MIGRATION.md) — isolated SQLite/PG17 CAS prototypes, schema dependencies, rollback, recovery and phased rollout.
- [Session writer inventory](SESSION_WRITER_INVENTORY.md) — identified legacy mutation paths, incarnation fencing, migration gates and remaining test gaps.
- [Agent turn contract](agents/AGENT_TURN_CONTRACT.md)
- [Background tool jobs](agents/BACKGROUND_TOOL_JOBS.md)
- [Skills lifecycle](skills/skills-lifecycle.md)

The imported runtime analysis and evidence remain in [runtime-decomposition](../runtime-decomposition/COMPARISON_PROTOCOL.md). For changes, verify the current source, tests, data compatibility and permission boundaries before treating earlier design notes as current guarantees.
