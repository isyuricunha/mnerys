# Architecture Documentation

The [Mnerys roadmap](../ROADMAP.md) defines the **target** Python Agent Core, authoritative Gateway and shared CLI/TUI/Web interfaces. The M1 audit documents existing inherited behavior and **proposes** a shared contract; neither the standalone Gateway nor the new CLI/TUI is implemented yet.

- [M1 audit of the inherited runtime](AGENT_CORE_GATEWAY_M1_AUDIT.md) — verified source map, existing security seams and extraction order.
- [Proposed Gateway / Agent Core contract](GATEWAY_AGENT_CONTRACT.md) — draft entities, responsibilities, events, approvals and compatibility gates.
- [Agent turn contract](agents/AGENT_TURN_CONTRACT.md)
- [Background tool jobs](agents/BACKGROUND_TOOL_JOBS.md)
- [Skills lifecycle](skills/skills-lifecycle.md)

The imported runtime analysis and evidence remain in [runtime-decomposition](../runtime-decomposition/COMPARISON_PROTOCOL.md). For changes, verify the current source, tests, data compatibility and permission boundaries before treating earlier design notes as current guarantees.
