# Mnerys Roadmap

> **Mnerys · Built to be yours.**
>
> **Status:** product roadmap and architectural direction, not a list of already shipped features.
> **Source of truth:** this document describes Mnerys itself. The archived [Odysseus roadmap](archive/odysseus/ROADMAP.md) is an inherited historical document and is **not** Mnerys's development plan.

## Product direction

Mnerys is an independent, open-source, self-hosted and modular AI platform. It began with a snapshot of Odysseus, but will own its agent runtime, memory, tools, integrations and user experience. Hermes Agent and OpenClaw are **architectural references, not runtime dependencies**.

**Approved direction:** combine a **Python-based agent core** and straightforward global command installation (inspired by Hermes) with a **central Gateway as the authority for sessions, agent execution and permissions** (inspired by OpenClaw). A CLI, terminal UI (TUI) and web workspace must use the **same application contracts**, not three unrelated agent implementations.

```text
           mnerys (global command)
                     |
         +-----------+-----------+
         |           |           |
        CLI         TUI         Web
         |           |           |
         +-----------+-----------+
                     |
          Mnerys Gateway / API
          auth · sessions · events
          approvals · permissions
                     |
              Mnerys Agent Core
         models · memory · tools
         MCP · skills · workflows
                     |
           Pluggable persistence
            SQLite / PostgreSQL
```

The diagram is a **target architecture**, not a claim that all components are already decoupled.

## Current foundation

| Area | Status | Reality |
| --- | --- | --- |
| Independent Mnerys repository and branding | Implemented foundation | Imported Odysseus code is maintained independently; inherited capabilities still need refactoring |
| Web workspace and initial agents, tools, MCP integrations | Inherited foundation | Existing code is functional in part, but is not yet a unified independent Mnerys Core |
| Private native development preview | Running | Versioned source, systemd and a stable private gateway, with deploy validation and rollback |
| Persistence | Partially implemented | SQLite remains the installation default; PostgreSQL 17 is supported for the primary database and used by the maintainer preview; auxiliary stores remain separate |
| Global `mnerys` executable | Planned | No public installation contract or packaged command yet |
| Mnerys CLI and TUI | Planned | Command examples below are proposals, **not currently available** |
| Unified Gateway and permissioned agent core | Planned | Existing interfaces/runtime must be audited and adapted |

See [native preview deployment](deployment/NATIVE_PREVIEW.md) and [PostgreSQL 17 compatibility](deployment/POSTGRESQL17.md) for verified infrastructure details and limitations.

## Milestones

### M1 — Audit and define the shared runtime contract

**In progress.** The [runtime audit](architecture/AGENT_CORE_GATEWAY_M1_AUDIT.md) and [shared contract proposal](architecture/GATEWAY_AGENT_CONTRACT.md) are documented. The detached-run manager supports tested, opt-in process-local SSE replay cursors. The [M1 regression baseline](architecture/M1_CONTRACT_TEST_BASELINE.md) now also freezes inherited Web owner/scope checks across browser and bearer clients and exact-run cancellation. Durable events and the Gateway service contract remain planned; runtime extraction, independent cross-transport integration and acceptance tests are pending.

- Inventory existing agent loop, model adapters, tool execution, MCP, skills, memory, session state and background jobs.
- Identify coupling to web routes, local files, storage, environment variables and global singletons.
- Define interfaces between **Gateway/API**, **agent core**, **persistence**, **tools/permissions** and **clients**; keep infrastructure and provider integrations optional.
- Specify canonical session, agent, turn, event and tool-approval contracts; support resumable streams and explicit cancellation.
- Reuse working imported capabilities where justified, with regression tests and backward-compatible migrations rather than a wholesale rewrite.

**Done when:** one documented, testable contract can serve multiple frontends without bypassing tool authorization; existing sessions and settings continue working.

### M2 — Mnerys Agent Core and Gateway

**Planned.**

- Establish a reusable **Python Agent Core** for model invocation, agent lifecycle, tool execution, memory, MCP, skills and workflow coordination.
- Make the **Gateway** the central authority for remote clients: authentication, session ownership, agent/turn management, streaming events, tool permissions and auditable approvals.
- Support local and remote model providers with configurable integrations and explicit failure states.
- Ensure background jobs, agent tools and client requests obey the same authorization rules; never trust a UI or CLI client as the permission authority.
- Separate persistence from deployments. Support SQLite by default and PostgreSQL where verified, while tracking remaining auxiliary-store migrations explicitly.

**Done when:** a controlled agent turn can be started, observed, approved/denied, resumed or cancelled through the same tested Gateway contract consumed by clients.

### M3 — Global `mnerys` CLI and packaging

**Planned.**

- Package the CLI as a real Python console-script entry point (for example `[project.scripts]` in `pyproject.toml`); avoid hand-written symlink hacks or requiring `sudo` for regular client installs.
- Support per-user global installation, e.g. via `uv tool install`, with documented installation, upgrades, uninstallation and version checks.
- Keep the **remote client lightweight**: it must not need the complete web server, database stack, schedulers or local models.
- Provide **remote mode** (authenticated connection to an existing Mnerys Gateway) and **local mode** (local agent runtime, when its dependencies are installed and configured).
- Maintain configuration profiles and secure credential storage; no secrets in command history, URLs printed in logs, public examples or Git.
- Offer reproducible behavior on Linux, macOS and Windows (including PowerShell); automated checks must validate the actual supported platforms.

**Proposed command UX — not implemented yet:**

```bash
mnerys                      # Open the TUI in an interactive terminal
mnerys tui                  # Explicitly open the TUI
mnerys chat "Olá"           # Non-interactive agent turn
mnerys agents list          # Inspect configured agents
mnerys models list          # Inspect available models
mnerys tools list           # Inspect authorized tools
mnerys connect              # Configure/select a remote Gateway profile
mnerys serve                # Run a configured local server
mnerys doctor               # Diagnose installation and connectivity
mnerys --version            # Print CLI version
```

Running `mnerys` without arguments should open the TUI **when attached to an interactive terminal**. In scripts/non-interactive contexts, present useful usage information or an explicit error rather than unexpectedly launching an interactive process. Final commands, options and packaging details will be confirmed during implementation.

**Done when:** a fresh user-level install exposes `mnerys` on the PATH, supports a real authenticated remote chat/agent turn, and uninstalls cleanly without disturbing server data.

### M4 — First-party terminal UI (TUI)

**Planned.**

- Build a keyboard-first, responsive TUI using the **same Gateway and session/event contracts** as CLI and Web.
- Include conversation history, session switching, agent/model selection, streamed replies, tool-call status, approval requests, cancellation and reconnection.
- Use the Mnerys visual language (initially **Eclipse**) while respecting terminal color limitations, accessible contrast and reduced-motion preferences.
- Keep TUI presentation technology an implementation decision: choose only after evaluating maintenance, platform support, client weight and interoperability with the Python package.
- Do not fork the agent core or independently implement permission decisions in the TUI.

**Done when:** a user can switch between Web and TUI and continue the same authorized conversation without losing state or bypassing approvals.

### M5 — Web, CLI and TUI parity

**Planned.**

- Adapt the inherited web experience to the agreed core/Gateway contracts incrementally.
- Align authentication, tool approvals, agent configuration, streaming turn state and error reporting across all clients.
- Persist user profile, selected agent, memory preferences and session context consistently across devices and interfaces.
- Validate real desktop and mobile Web behavior, keyboard-only TUI navigation and scripted CLI operation.
- Add integration tests that deliberately try to bypass tool permissions from every client.

**Done when:** the same session and agent state can be accessed safely across all three interfaces, with consistent capabilities and denial behavior.

### M6 — Installer, operational reliability and first releases

**Partially implemented.** CI-gated semantic-release published the initial [v0.1.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.1.0) and the AMD64/ARM64 GHCR image build completed successfully. The [release automation](deployment/RELEASE_AUTOMATION.md) and [0.1.0 notes](releases/0.1.0.md) document this foundation; clean-machine installation, runtime compatibility and restore validation remain pending.

- Document distinct installation profiles: **lightweight remote CLI/TUI client**, **native full server**, and **optional Docker Compose**.
- Maintain a persistent private preview over Tailscale with a stable endpoint, real checks, controlled restarts and verified rollback. **Strict zero downtime is not a requirement.**
- Provide backup/restore and upgrade/migration paths for each supported persistence backend. Test PostgreSQL 17 restoration rather than assuming backup files are usable.
- Define stable CLI configuration and API compatibility/versioning before public distribution.
- Use Conventional Commits and GitHub Actions + semantic-release starting from `0.1.0`, when release criteria are met. Maintain version-family container tags such as `v0`, `v1` and `v2` in addition to immutable version tags.
- Preserve AGPL-3.0 obligations and third-party notices. Keep the mascot artwork under its separate license.
- Run reliability, security and installation checks before promoting a public release.

**Done when:** a clean machine can install each documented profile, connect to the correct backend, recover from a failed update, and use published releases without any private maintainer configuration.

## Engineering principles and non-goals

- **Single source of truth for agent behavior:** no duplicated Python/TypeScript agent engines just to support different frontends.
- **Permission authority lives server-side:** auditable, granular approvals for local tools, remote tools and MCP; untrusted skills, webpages and memories never override authority.
- **Privacy by default:** no internal IPs, Tailscale names, hostnames, credentials, topology, real user content or environment dumps in public docs, logs, fixtures, commits or screenshots.
- **Small minimal installation:** optional integrations must not force a large dependency stack.
- **Durable changes:** schema compatibility, migrations, integration tests, observability, safe rollback, existing settings and user data are prioritized over quick rewrites.
- **Not a fork tracking upstream:** Hermes and Odysseus are references, not dependencies that determine Mnerys's future.
- **No unnecessary zero-downtime complexity:** deploys use validation and rollback, with brief controlled downtime when necessary.

## Roadmap maintenance

- Every milestone is **planned** until merged code, tests and verified runtime behavior justify marking it implemented.
- Record key design decisions and migration constraints under `/docs` alongside code changes. Keep the README linked to this roadmap.
- Public docs use placeholders; operational/private addresses and credentials stay outside Git.
- The inherited [Odysseus roadmap](archive/odysseus/ROADMAP.md) is retained for provenance. **Do not add Mnerys plans to that historical document.**
