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

**In progress — M2.1 internal authority extraction only; an independent authenticated Gateway remains planned.** The [ordered M2 execution and acceptance plan](architecture/M2_GATEWAY_IMPLEMENTATION_PLAN.md) follows the completed [M1 design exit](architecture/M1_EXIT_REVIEW.md). The existing Web stream route now delegates initial and exact-approval continuation authority preparation to an admission-bound internal service. The Gateway now validates owner/session, decision scope and plan mode, and consumes approval decisions through the inherited sealed store. The Web adapter still authenticates the HTTP request, updates the visible approval card and restores continuation context. Both Chat paths now resolve owner-scoped foreground fallback through the admitted Gateway facade, and enabled fallbacks use admission-bound, per-model context shaping without premature persistence. The final prompt handoff now also checks the admitted context owner, selects strict or route-neutral history, and tags internal tool content as untrusted data without minting user intent. The transport-neutral context composition algorithm now lives in [`src/gateway_chat_context.py`](../src/gateway_chat_context.py) and requires explicit trusted service ports. The Web compatibility adapter resolves the authenticated request identity, enforces admission and emits legacy Web events. Both Web routes require their issued admission to match the session and HTTP principal before preprocessing or message persistence. The shared builder now depends on [explicit context service ports](architecture/M2_CONTEXT_SERVICE_PORTS.md) for preferences, preprocessing, attachment manifests, memory/RAG prefaces, message writes, context compaction, cache-first model normalization, history retrieval, date/time, source attribution and token-window sizing, all backed by the same inherited implementations. Pure context-selection rules and result types now also live in `src/`; the legacy Web module re-exports compatible entry points and implements the only production adapter. Context composition is **transport-independent within the current Python process**, but the full turn lifecycle, memory/RAG services and durable Gateway transport are not yet standalone. Stream delivery is now selected by an admission-bound Gateway service: interactive turns retain detached/resumable execution, while Compare and unattended native streams remain directly tied to client disconnects. The Web route still renders the existing SSE response and headers. Post-response extraction and completion jobs now pass through an admission-bound Gateway dispatch that validates session and context ownership before invoking the unchanged, sequential legacy task runner; durable job scheduling remains planned. The final tool-policy composition is now Gateway-admitted and owner-checked, combining account, delegated-token, global, Compare, Plan and guide-only restrictions after inherited request-intent routing. Early compact-runtime eligibility and late contract/native-workspace eligibility now also use an admitted, owner-checked Gateway service. The Gateway now checks the bound request authority before invoking the original immutable routed-contract resolver, with no new grants. Compact preview/experiment scoping and final runtime denylist reconciliation now also use an owner-checked Gateway service, retaining the original policy-filtered resolver and model-scoped experiment rules. Native/MCP schema inventory preparation, delegated and owner-scoped exclusions, and the existing SFT Web filesystem guard now also pass through an admitted Gateway service. Exact selected-tool routing and typed browser/Web follow-ups, visible-document/editor requirements, and fixture-only personal-family restoration now use a separate owner-admitted Gateway selection service. Initial candidate-family classification and guarded warm-browser/document augmentation now also pass through a session-admitted Gateway service. Post-capability public-Web intent reconciliation, inherited opt-in toggle gates and initial browser/Web denials now use a separate admitted Gateway service, with lazy server-owned typed history evidence. Preliminary Chat-to-Agent selection now uses a pure, non-authorizing Gateway helper before session admission, preserving the inherited action classifier and workspace hint. Initial lexical public-Web, URL, discovery, personal-store and interactive-browser intent now also uses a pure, pre-admission Gateway classifier with the original helper names reexported by Web; the Web adapter still resolves and confines actual workspace paths, supplies lazy local-rendering predicates and formats image-edit context. Post-session Chat-to-Agent routing from the persisted compact model, typed Web/browser history and confined workspace paths, plus active-document editing promotion after owner validation, now use admitted Gateway services; Web still owns the corresponding source/history resolvers and document access checks. Effective runtime UI-toggle projection now also runs under session admission after final account/global policy: the Gateway overwrites untrusted client toggle claims before request authority is constructed, preserving the original RAG/research request-toggle semantics without creating grants. The Agent Loop is now entered through an owner-admitted Gateway execution boundary that validates the loaded model/session, request authority and final tool denials, reusing the inherited TurnService and dispatcher. A synthetic no-HTTP Gateway turn is tested; the Web route still composes upstream context, policy and persistence. A first one-call direct composer now assembles admitted text context, policy and authority before invoking that same Agent Loop, **with all tools blocked and no conversation persistence**. This proves the integration path but is not a production Web cutover. Five existing streaming terminal assistant-response saves now pass through an owner-admitted Gateway check before calling the unchanged legacy writer, preserving incognito, metadata and message-ID behavior; both cancellation partials are now admission-checked through the original writer, and completed streams guard against duplicate DONE/late-disconnect saves; durable idempotence and atomic whole-turn persistence remain open. The [direct text compositor](../src/gateway_turn_composer.py) now also has a [filtered, process-local typed event adapter](architecture/M2_1_TYPED_TURN_EVENTS.md) with an in-memory completion outcome; the Agent Core can now emit native completion signals for this admitted, tools-disabled direct path while the evidence and teacher gates preserve terminal semantics. The Core's evidence/completion gate also now emits native filtered visible text, safe final metrics and redacted failures for that restricted direct mode; a separate admitted runtime option now emits native filtered tool lifecycle metadata and sealed approval references, while provider ingress and other event families remain on legacy SSE and the production Web path is unchanged. This metadata contract does not expose raw tool results or complete approval UX. The same direct text composer now accepts an explicit server-owned native tool surface, composes the original inventory, capability/intent selection, authority and immutable contract, and exercises the actual Loop with a synthetic immutable-read fixture. Tools remain disabled by default; the default direct profile remains non-persisting, while an explicit server-trusted text-only profile now saves user messages through the original context ports and completed assistant replies through the inherited admitted writer. An optional admitted interrupted writer now saves filtered visible partials on cancellation or failure; terminal completion is not exposed before the legacy assistant write succeeds. Disposable SQLite and PostgreSQL 17 integration now verifies the inherited session writer, absent post-commit message receipts, deleted sessions and stale cached ownership before acknowledging a direct turn. The PostgreSQL 17 cases use a separate loopback-only, tmpfs-backed Docker container and dedicated test credentials; this does not validate production migration or concurrency fencing. This path still has no atomic whole-turn transaction, durable retry receipt or cross-run idempotence; MCP, subprocess/browser and external-filesystem approval continuation, Rich media attachments and full Web fallback/parity acceptance remain open. A restricted direct reference-only text-upload profile now uses the inherited owner-scoped upload resolver, manifests and text processor, with bounded identity/digest rechecks and prompt-injection-safe untrusted context; image, audio, PDFs, uploads with tools, and multimodal persistence remain unsupported. The direct typed stream now reports source-owned fallback route selections (not provider-authored SSE), without endpoint credentials or durable model attribution. The direct composer now has an opt-in, trusted foreground fallback profile using the existing owner-scoped preferences, server-allowed models, selected-session primary and inherited Agent Loop candidate handling; synthetic HTTP 503 recovery, noneligible failures, visible-output pinning and cancellation have direct acceptance tests. Native approval controls now reuse the same composer, sealed store, authority snapshot and dispatcher; a waiting approval produces an explicit pause outcome, and approved reads are not rescheduled. A trusted session-workspace profile now resumes native backend approvals only when the server-selected path still matches the loaded session workspace and the original sealed approval; exact sealed document edit/update/suggest approvals now require an owner/session-bound active document with unchanged version and content digest, verified with disposable SQLite/PostgreSQL 17; native filesystem approvals now require previously sealed, same-workspace resource identities, with unchanged roots/targets and existing dispatcher policy; remote or unbound filesystem resources remain unsupported in the direct profile. Denial, replay, expiry, current-policy vetoes and cancellation are tested without a live provider or production data. A shared process-local typed run path now reuses the inherited Web run manager for exact-run Stop, finite replay cursors, subscriber reconnect and terminal-after-writer ordering. An opt-in, no-HTTP strict-text Web SSE compatibility adapter now reuses the same composer and original assistant writer, exposing only a confirmed saved-message ID and filtered browser-understood frames; no production Web switch, durable event-store cursor or cross-restart replay exists. The remaining [M2.1 exit gates](architecture/M2_1_EXIT_GATE.md) require fully featured turn composition/persistence, native typed Agent Core events and direct-versus-Web acceptance. Durable turn events and an independent transport remain planned.

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
