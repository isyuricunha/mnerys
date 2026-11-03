# Architecture Documentation

The [Mnerys roadmap](../ROADMAP.md) defines the **target** Python Agent Core, authoritative Gateway and shared CLI/TUI/Web interfaces. The M1 audit documents inherited behavior and **proposes** a shared contract. Opt-in process-local SSE replay cursors, the first internal TurnService extraction, typed Gateway session admission, a read-only session ownership lookup port, account privilege-denial preparation and shared message persistence serialization are implemented. Experimental incarnation-fenced background delivery and cache reconciliation are tested **without production activation**. These seams reuse the inherited agent loop and run manager; a standalone authenticated Gateway, durable events and Mnerys CLI/TUI are not implemented yet.

- [M1 audit of the inherited runtime](AGENT_CORE_GATEWAY_M1_AUDIT.md) — verified source map, existing security seams and extraction order.
- [Proposed Gateway / Agent Core contract](GATEWAY_AGENT_CONTRACT.md) — draft entities, responsibilities, events, approvals and compatibility gates.
- [M1 contract regression baseline](M1_CONTRACT_TEST_BASELINE.md) — Web owner/scope/cancellation characterization and shared admission regressions, plus remaining integration gaps.
- [Session revisions and concurrency migration](SESSION_REVISION_MIGRATION.md) — isolated SQLite/PG17 CAS prototypes, schema dependencies, rollback, recovery and phased rollout.
- [Session writer inventory](SESSION_WRITER_INVENTORY.md) — identified legacy mutation paths, incarnation fencing, migration gates and remaining test gaps.
- [Agent turn contract](agents/AGENT_TURN_CONTRACT.md)
- [Background tool jobs](agents/BACKGROUND_TOOL_JOBS.md)
- [Skills lifecycle](skills/skills-lifecycle.md)

The imported runtime analysis and evidence remain in [runtime-decomposition](../runtime-decomposition/COMPARISON_PROTOCOL.md). For changes, verify the current source, tests, data compatibility and permission boundaries before treating earlier design notes as current guarantees.
