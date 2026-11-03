# Architecture Documentation

The [Mnerys roadmap](../ROADMAP.md) defines the **target** Python Agent Core, authoritative Gateway and shared CLI/TUI/Web interfaces. The following documents describe **existing inherited behavior**, not proof that the target Core/Gateway separation has been implemented.

- [Agent turn contract](agents/AGENT_TURN_CONTRACT.md)
- [Background tool jobs](agents/BACKGROUND_TOOL_JOBS.md)
- [Skills lifecycle](skills/skills-lifecycle.md)

The imported runtime analysis and evidence remain in [runtime-decomposition](../runtime-decomposition/COMPARISON_PROTOCOL.md). For changes, verify the current source, tests, data compatibility and permission boundaries before treating earlier design notes as current guarantees.
