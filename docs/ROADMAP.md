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
| Unified release versioning | Implemented | One version authority: semantic-release tags drive the CLI distribution at build time, and every release attaches a verified, version-matched CLI wheel and sdist |
| Global `mnerys` executable | Planned | No public installation contract or packaged command yet |
| Mnerys CLI and TUI | Planned | Command examples below are proposals, **not currently available** |
| Unified Gateway and permissioned agent core | Planned | Existing interfaces/runtime must be audited and adapted |

See [native preview deployment](deployment/NATIVE_PREVIEW.md) and [PostgreSQL 17 compatibility](deployment/POSTGRESQL17.md) for verified infrastructure details and limitations.

## Milestones

### M1 — Audit and define the shared runtime contract

**Completed for audit and contract design (2026-11-03); not an operational Gateway.** The [M1 exit review](architecture/M1_EXIT_REVIEW.md) records the accepted design scope, test evidence and non-negotiable [M2 delivery gates](architecture/M2_GATEWAY_IMPLEMENTATION_PLAN.md). The [runtime audit](architecture/AGENT_CORE_GATEWAY_M1_AUDIT.md) and [shared contract proposal](architecture/GATEWAY_AGENT_CONTRACT.md) document the extraction strategy.

**Implemented internal seams:** process-local SSE replay cursors, the [M1 regression baseline](architecture/M1_CONTRACT_TEST_BASELINE.md), the [TurnService extraction](architecture/GATEWAY_AGENT_CONTRACT.md#m1-incremental-implementation-internal-turnservice-seam), [typed principal/session admission](architecture/GATEWAY_AGENT_CONTRACT.md#m1-incremental-implementation-trusted-principal-and-session-admission), [read-only ownership repository plus account privilege limits](architecture/GATEWAY_AGENT_CONTRACT.md#m1-incremental-implementation-ownership-lookup-and-privilege-limits), and a [shared persisted-message preparation boundary](architecture/SESSION_WRITER_INVENTORY.md#incremental-live-compatibility-extraction-message-preparation). Existing Web run controls require an admitted session. The inherited SessionManager, history replacement and background-job delivery now share message serialization, but still own their existing transactions and unversioned writes. The source-of-truth database and hardened tool dispatcher remain unchanged; global/model/intent policy orchestration still follows inherited paths.

**Implemented as isolated prototypes only:** [revision- and incarnation-fenced, owner-scoped session metadata/message updates](architecture/SESSION_REVISION_MIGRATION.md), [atomic background-job delivery and cache reconciliation](architecture/SESSION_WRITER_INVENTORY.md#experimental-fenced-delivery-and-cache-reconciliation) have passing disposable SQLite/PostgreSQL 17 tests for concurrency, idempotency, rollback and synthetic migration preview. The [writer inventory](architecture/SESSION_WRITER_INVENTORY.md) maps remaining legacy integration paths. Live compatibility fixes now prevent failed transcript replacements from publishing uncommitted IDs, roll back single-message cache appends when persistence fails, isolate unmanaged Session objects from the process-wide persistence manager, and keep agent session-tool metadata cache updates post-commit and owner-matched. Agent rename no longer makes an unnecessary second, unscoped database write. **None of the revision-fenced prototypes are enabled** against the live Session or job schemas; full migrations, backups/restores and all-writer integration are still required.

**Deferred to M2 and later milestones:** the operational authenticated Gateway/API, full owner-scoped session CRUD/revision repository, extracted model/intent/context orchestration, durable turn/event/approval records, persistent fencing, cross-client integration and end-to-end acceptance tests. The proposed contract is a design artifact, not a shipped API.

- Inventory existing agent loop, model adapters, tool execution, MCP, skills, memory, session state and background jobs.
- Identify coupling to web routes, local files, storage, environment variables and global singletons.
- Define interfaces between **Gateway/API**, **agent core**, **persistence**, **tools/permissions** and **clients**; keep infrastructure and provider integrations optional.
- Specify canonical session, agent, turn, event and tool-approval contracts; support resumable streams and explicit cancellation.
- Reuse working imported capabilities where justified, with regression tests and backward-compatible migrations rather than a wholesale rewrite.

**M1 design exit:** the audited runtime boundaries, proposed reusable contract, authorization invariants, writer/migration map and bounded regression baseline are documented and tested as described in the [exit review](architecture/M1_EXIT_REVIEW.md). **Runtime proof that two real adapters share a permissioned, restart-safe Gateway is an explicit M2 acceptance gate**, not a claim made by this milestone. Existing Web/session compatibility remains mandatory during implementation.

### M2 — Mnerys Agent Core and Gateway

**In progress — M2.1 through M2.5 implemented, published, rolled out to
the private preview, and the Web execution path is now the shared
Gateway composition (November 6, 2026).** The authorized preview
rollout applied both migrations to the PostgreSQL 17 primary database
after a verified backup and isolated restore test, deployed the
published revision with all native runner checks passing, and the owner
validated the logged-in chat before the authorized
`MNERYS_M21_WEB_CORE_CUTOVER` activation; the composition now serves
the preview's Web chat and Agent turns end to end.

**M2.1 (authoritative internal turn boundary).** One owner-admitted,
transport-neutral turn composer now covers the full production Web feature
set: server-selected context features (memory, RAG, web, incognito), a
bounded multimodal attachment profile with post-preprocess identity
rechecks, active-email review context, presets/thinking/reasoning/tool
budget options, plan/compare/search context, generalized persisted native
tool surfaces, and a typed display event contract (`thinking.delta`,
`agent.step`, `turn.control`, `sources.web`) with an optional trusted
`client.frame` passthrough and a fail-closed browser serializer. The actual
`/api/chat_stream` route can run ordinary chat/agent turns through
`GatewayTurns.compose_web_turn` behind the disabled-by-default
`MNERYS_M21_WEB_CORE_CUTOVER` switch, with the legacy chain preserved
verbatim. A controlled real-provider acceptance turn passed through the
composer against the private preview's authorized NVIDIA NIM endpoint.
Evidence: [M2.1 exit gate](architecture/M2_1_EXIT_GATE.md).

**M2.2 (durable session writers and concurrency).** Session
revision/incarnation fencing is integrated across the inherited
SessionManager (appends, truncation, replacement, renames, archives,
importance, touches, empty-session cleanup, fresh incarnations on create)
and background job delivery (origin-incarnation registration, recreated
session discard, guarded counter updates). The additive schema and its
explicit, approval-gated migration are implemented; unmigrated rows keep
the legacy write path. Disposable SQLite and PostgreSQL 17 suites cover
concurrency, owner transfer, recreate, fresh-interpreter restart and
legacy compatibility, and a disposable PG17 backup/restore drill preserves
the fenced schema. Plan: [session revision migration](architecture/SESSION_REVISION_MIGRATION.md).

**M2.3 (durable turns, events, approvals).** Owner-scoped turn records,
ordered versioned sanitized events with immutable per-turn sequences and
explicit gaps, exactly-once deterministic terminals, sealed one-use
approval records bound to owner/session/incarnation/turn/tool/digest/
resource/authority with expiry, idempotency keys, restart-safe reads and
bounded retention pruning are implemented under an explicit migration.
The typed adapter and approval decision boundary persist through the same
store when enabled. Documentation: [durable turns](architecture/M2_3_DURABLE_TURNS.md).

**M2.4 (authenticated Gateway API).** The versioned `/api/v1` contract is
implemented: authenticated owner-scoped turns with idempotent creation,
snapshots, sanitized ordered SSE replay, exact-turn cancellation, one-use
approval decisions through the shared sealed store (delegated tokens
refused by design), owner-scoped session listing and capability discovery
that never grants rights. The Web adapter and an independent scripted
remote client (httpx only, no server/DB/loop imports) share the same
Gateway facade, detached run manager and durable records; no parallel
agent or permission path was introduced. The Web application keeps its
existing authenticated endpoints; the v1 router is available for the
authorized control-plane rollout.

- Establish a reusable **Python Agent Core** for model invocation, agent lifecycle, tool execution, memory, MCP, skills and workflow coordination.
- Make the **Gateway** the central authority for remote clients: authentication, session ownership, agent/turn management, streaming events, tool permissions and auditable approvals.
- Support local and remote model providers with configurable integrations and explicit failure states.
- Ensure background jobs, agent tools and client requests obey the same authorization rules; never trust a UI or CLI client as the permission authority.
- Separate persistence from deployments. Support SQLite by default and PostgreSQL where verified, while tracking remaining auxiliary-store migrations explicitly.

**Done when:** a controlled agent turn can be started, observed, approved/denied, resumed or cancelled through the same tested Gateway contract consumed by clients.

### M3 — Global `mnerys` CLI and packaging

**Operationally verified (November 6, 2026).** The `mnerys` command is a
real Python console-script package (`packages/mnerys-cli`) with `httpx` as
its only runtime dependency; remote-only installs carry no web server,
database stack or agent runtime. It supports per-user installation (`uv
tool install`, or `pip install .`), named profiles with restrictive
credential storage, a remote mode against the authenticated Gateway API
v1 (chat streaming with terminal events and cancellation, discovery,
doctor), a local mode (`mnerys serve` boots the same application the
preview runs, and local chat uses the identical Gateway contract),
explicit exit codes, and clean install/upgrade/uninstall. The private
preview runs the authorized M3 revision with API v1 mounted on the real
application; an installed CLI completed authenticated, provider-backed
turns (including PT-BR and Unicode), cancellation, persistence, turn
records, ownership isolation and idempotency checks against that live
Gateway. Public PyPI publication remains a separate future decision;
until then the package installs from the repository or its built
distributions.

**Done when:** a fresh user-level install exposes `mnerys` on the PATH,
supports a real authenticated remote chat/agent turn, and uninstalls
cleanly without disturbing server data. Verified; see [CLI
documentation](cli.md) and the [M3 acceptance
evidence](architecture/M3_ACCEPTANCE.md).

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

**Partially implemented.** CI-gated semantic-release published the initial [v0.1.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.1.0) and the AMD64/ARM64 GHCR image build completed successfully. The subsequent [v0.2.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.2.0) and its ARM64/AMD64 container-build and version-tag workflow jobs have completed successfully after CI. Clean-machine installation and runtime image validation remain separate acceptance work. The [release automation](deployment/RELEASE_AUTOMATION.md) and [0.1.0 notes](releases/0.1.0.md) document the foundation; clean-machine installation, runtime compatibility and restore validation remain pending.

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
