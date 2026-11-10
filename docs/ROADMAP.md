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
| Global `mnerys` executable | Implemented | The `mnerys` CLI is packaged (`packages/mnerys-cli`) and every release publishes a verified, version-matched wheel and sdist; install from GitHub Release assets or the repository |
| Mnerys CLI | Implemented | Real console-script package with profiles, remote Gateway turns, a local `serve` mode and explicit exit codes; see [cli.md](cli.md) |
| Mnerys TUI | Implemented | Keyboard-first Textual interface on the same Gateway/Agent Core, sessions, events and approvals; see M4_ACCEPTANCE.md |
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
until then the package installs from the repository or the release assets
published with every release.

**Done when:** a fresh user-level install exposes `mnerys` on the PATH,
supports a real authenticated remote chat/agent turn, and uninstalls
cleanly without disturbing server data. Verified; see [CLI
documentation](cli.md) and the [M3 acceptance
evidence](architecture/M3_ACCEPTANCE.md).

### M4 — First-party terminal UI (TUI)

**Complete.** The implementation baseline is recorded in
[M4_TUI_IMPLEMENTATION_PLAN.md](architecture/M4_TUI_IMPLEMENTATION_PLAN.md)
(framework decision: **Textual 8.x**; architecture, streaming model,
security model and acceptance matrix). Submilestones below are formal:
each completes only when its exit gate passes with evidence; ordinary
commits are not milestones.

- **M4.1 Gateway client surface:** owner-admitted v1 endpoints for session
  creation, session metadata + persisted history, approval inspection and
  the session model preference; async client methods in the CLI package.
  *Gate:* negative admission tests against the real router and the CLI's
  session-creation path working against it.
- **M4.2 TUI foundation:** Textual app shell, `mnerys` interactive entry,
  `mnerys tui [--profile]`, Eclipse theme, help and shortcut discovery,
  graceful exit, optional `mnerys-cli[tui]` extra. *Gate:* headless
  `run_test` boots the installed wheel's TUI; base install stays
  lightweight.
- **M4.3 Conversation experience:** DONE. Persisted history rendering,
  streamed turns with ordered, idempotent event application, cancellation,
  reconnection with cursor replay, multiline compose, safe Markdown/code
  blocks. *Gate passed:* streaming, cancellation and replay matrix rows and
  the escape-injection tests (see M4_ACCEPTANCE.md).
- **M4.4 Sessions and discovery:** DONE. Session list/create/switch with
  history reload and server-validated model selection. *Gate passed:* Web/TUI
  continuity rows against the real router in isolation (see M4_ACCEPTANCE.md).
- **M4.5 Tools and approvals:** DONE. Tool status rendering, human-readable
  approval cards, decisions through the sealed server store, denial/expiry/
  replay handling. *Gate passed:* approval matrix rows with server authority
  intact (see M4_ACCEPTANCE.md).
- **M4.6 Acceptance, packaging and publication:** DONE. Full acceptance
  matrix evidence, cross-platform CI results, packaging, docs, website and
  the release. *Gate passed:* every M4 exit requirement evidenced; v0.15.1
  published with version-matched wheel and sdist artifacts; website updated.

The TUI uses the same Gateway/session/event contracts as CLI and Web, the
Mnerys visual language (initially **Eclipse**) with terminal-appropriate
contrast and reduced-motion support, and no fork of the agent core or any
independent permission decision.

**Done when:** a user can switch between Web and TUI and continue the same authorized conversation without losing state or bypassing approvals, all gates have evidence, and docs/website reflect the verified implementation.

**M4 stabilization pass (November 2026, post-release):** the private-preview
acceptance of v0.15.2 exposed turn-execution and transcript-consistency
defects; the stabilization fixes (single assistant persistence per turn,
stale running-turn reconciliation at startup, retained typed failure
tracebacks, shared AuthManager for owner checks, lifecycle transcript
invariants proven on disposable SQLite and PostgreSQL 17) shipped in
v0.15.3 and follow-ups; see M4_ACCEPTANCE.md and
DUPLICATE_TRANSCRIPT_INVENTORY.md.

**Runtime reliability pass (November 2026, maintenance after M4):** live
preview evidence tied a Web-answered tool approval to an HTTP 500 while
the agent stayed waiting (the approval continuation arrived with an
empty composer text and the composed turn admitted it too early). The
fixes shipped with the runtime reliability stabilization: approval
continuations compose from the sealed record with real HTTP error
mapping, the gateway model snapshot no longer reads a nonexistent
column, and provider-reported cache/reasoning token counts are visible
end to end (usage events, turn metrics, persisted metadata); see
GATEWAY_AGENT_CONTRACT.md and CACHE_INVENTORY.md. The historical
duplicate-transcript repair itself completed earlier as reversible
logical hiding (see DUPLICATE_TRANSCRIPT_INVENTORY.md).

**Approval continuation repair (preview verified):** a preview
approval reached the process producer but failed because rebinding the same
request made it appear inherited. The repair distinguishes that
rebinding from a child delegation, rechecks claim expiry, and restores the
Gateway's continuation mode and workspace in the Web adapter. Disposable
regressions and the private preview exercise real approved shell execution
and Mistral continuation. Revision `cc76180dac73` also passed the real denial,
persisted-card resolution and HTTP 409 replay checks. The installed CLI and
TUI continued the same provider-backed conversation with two completed
durable v1 turns and no duplicate transcript rows. The complete CI matrix is deferred until
publication. General Web stream replay and pending approvals
remain process-local across server restarts; durable Gateway v1 event records
do not cover that general Web stream. See the
[authority contract](architecture/GATEWAY_AGENT_CONTRACT.md#approval-continuation-authority-preview-verified).

### M5 — Agent capabilities, extensibility and personal experience

**Planned — scope reorganized in November 2026. No M5 submilestone is
complete or published merely because an inherited capability exists.**
This is the new M5 requested after M4: consolidate the agent's behavior
around the single Python Core and authenticated Gateway, improve the
developer loop, then deliver natural filesystem/terminal operation,
Hermes-style approvals, extensible tools, providers, identity/memory,
Telegram, onboarding and automation. Hermes, OpenClaw, Claude Code, Codex
and Gemini CLI are research references, not runtime dependencies.

**Cross-cutting rule:** each implemented capability must use the same
server-enforced authorization and session/turn contracts from CLI, TUI,
Web and Telegram where that client is available. Do not build another
agent engine per client. CLI/TUI/Web exist today; Telegram's first-class
adapter and onboarding remain planned. Preview fixes do not by
themselves complete M5 or release an unverified feature.

#### M5.1 — Fast development loop and recoverable run state

**Complete.** The implementation architecture, security model, and exit
evidence are recorded in
[M5_1_STATE_RECOVERY.md](architecture/M5_1_STATE_RECOVERY.md). The default
local validation command (`scripts/dev/git_validation.py affected`) selects
changed module tests, source-reading fixtures, and reviewed contracts.
Recovery across server restarts and client disconnects is fully implemented:
interrupted runs atomically settle with unknown effects (never auto-replayed),
pending approvals retire as superseded, and historical UI cards project as
unavailable with no restored executable authority.

- Establish a permanent, version-controlled risk-based test policy:
  focused impacted-module and security/negative tests during development,
  relevant AST/exec/compile/required-read fixture checks, static/staged
  pre-commit checks and disposable SQLite/PostgreSQL 17 contract tests.
  Do **not** run all four pytest shards locally for every increment.
  Hosted GitHub CI retains the authoritative full test matrix and
  security jobs after publication; inspect and fix every failing job.
- Build and validate a conservative affected-test selector using
  dependency and contract mapping, with an explicit full-suite escape
  hatch. Never weaken assertions or silently skip directly affected
  tests. Native fast hooks already exist and must be preserved.
- Audit Web process-local stream replay, approval queues and Gateway v1
  durability. Persist sanitized turn/event/decision state where needed
  for restart-safe visibility and audit, but never restore executable
  authorization from a UI card, transcript or stale in-memory record.
  Interrupted operations with unknown effects remain **unknown**, not
  automatically replayed. Review cancellation, once-only claims,
  idempotency, expiry, pruning, reconnect and owner isolation.
- Preserve existing SQLite-default/PostgreSQL-17 support, backups and
  rollback; changes to live data require separate authorization.

**Exit gate passed:** tested selective-test workflow, full-CI mapping, and
documented recovery behavior across restart/disconnect (`tests/test_gateway_restart_recovery.py`);
negative authority and isolated durable-storage tests pass.

#### M5.2 — Anthropic-first native tools and safe file edits

- Use **one fixed, provider-independent model-visible tool registry**,
  following Claude Code's names and semantics: `Read`, `Write`, `Edit`,
  `Glob`, `Grep`, `Bash`, `WebFetch`, `WebSearch`, `ToolSearch`
  and `Skill`, as capabilities become available. Every supported model
  sees the same names, descriptions, argument contracts and result
  semantics for each available capability. Do not invent Mnerys-only
  names for these common operations or rename them by model/provider.
- **Only serialize that same registry into each provider's required API
  tool-calling protocol and normalize responses** (Anthropic Messages,
  OpenAI-compatible and other supported transports). These are wire
  format adapters, not tool-name aliases, alternate tool registries or
  separate implementations. Shared Python Gateway execution,
  admission, approvals, permissions and audit remain authoritative.
  Anthropic's versioned API-native `bash`/`text_editor` schemas are
  different from Claude Code's model-visible `Bash`/`Edit` names; do
  not silently substitute them or change the canonical vocabulary.
  Evaluate optional native API optimizations only if the exact public
  tool contract and cross-provider compatibility are preserved.
- Required semantic operations: file read (including line ranges),
  create/write, targeted edit, structured patch, directory listing,
  file-name glob and content search, shell execution, foreground/background
  process control, web search/fetch, browser interaction, and discovery/
  invocation of installed MCP tools and skills. Avoid replacing a safe
  native file operation with an arbitrary shell command.
- Implement directory listing through `Read`/editor `view` and file
  discovery through `Glob`/`Grep`; `Write` must distinguish creating
  a new file from overwriting one, while `Edit` provides precise
  replacement. Structured patch operations must use one documented,
  stable shared contract; do not add model-specific `apply_patch`
  aliases. Background process control, browser and MCP invocation retain
  consistent, documented capabilities rather than masquerading as file
  operations. `ToolSearch` and `Skill` provide discovery and activation;
  discovered MCP tools retain their server-defined namespaced names
  across all providers. MCP standardizes discovery/schema exchange,
  not a universal built-in tool vocabulary.
- File overwrite/edit protection: track a server-trusted, owner/session-
  scoped read baseline or version; reject edits on an unread or changed
  existing file, enforce atomic compare-and-write where supported, and
  provide actionable conflict feedback. Creating a new file is a separate
  operation. Evaluate symlinks, TOCTOU, paths outside CWD, and what claims
  remain enforceable when unrestricted shell commands can also write.
- Advertise only tools actually executable under the current model,
  client, user and mode policy; never fabricate tool calls/results from
  model prose. Validate native function calling, protocol serialization,
  approval routing, result handling, streaming and cache efficiency with
  real providers.

**Exit gate:** real provider-backed read/search/edit/patch/shell tests;
unread/stale file edits rejected; exact tool calls, permissions and
results agree between the model trace, Gateway and client UI. Verify
the **same canonical tool names and behavior** on at least two
different provider API protocols, with only wire-format translation.
No provider-specific renaming, duplicated tool catalog, silent tool
emulation or loss of results, authorization or streaming fidelity.

#### M5.3 — Natural CWD, permission UX and opt-in YOLO

- No mandatory per-chat workspace. Local CLI/TUI begin in the invoking
  shell's current directory; local Web and Telegram begin in the
  configured execution account's home. Remote CLI/TUI use the server's
  explicitly selected CWD, otherwise its configured/home fallback.
  A chosen per-session CWD persists and is consistently honored by file
  tools, shell, processes, agent context and approvals. Never infer a
  remote user's local filesystem or run as root implicitly.
- Provide the familiar Hermes-style choices: **allow once**, **allow for
  this session**, **always allow this specific scoped rule**, or **deny**.
  Centralize rules, provenance, expiry and revocation at the Gateway.
  Make a deterministic local command-risk classifier the default;
  optional smaller-model review requires explicit configuration, never
  a mandatory paid/external LLM. Detect destructive commands without
  trusting superficial string matching or treating a prefix like `rm`
  as an unlimited grant.
- Offer explicit, visible, opt-in YOLO mode per authorized profile/session,
  with a clear effective policy across all clients. YOLO may suppress
  ordinary user prompts but must **not** disable authentication,
  cross-owner isolation, OS boundaries, sandboxing or non-delegable
  privileges. Preserve exact-operation accounting and audit.
- Keep a unified **Agent-first** experience; remove the confusing
  Chat/Agent UI split once feature parity and migration are verified.
  Preserve legacy `mode=chat` API compatibility until clients migrate.

**Exit gate:** home/CWD fallback and session persistence verified in
local and remote transports; actual risky-command prompts and all four
decisions tested, plus explicit YOLO/deny/owner-boundary negative tests.

#### M5.4 — Model providers, metadata, reasoning and credentials

- Support validated provider adapters and account configurations:
  authorized Codex/ChatGPT subscription OAuth when terms allow,
  official Claude Code CLI/login integration only through permitted
  flows, NVIDIA NIM, FreeInference, OpenAI-compatible endpoints,
  generic API keys, local endpoints and other supported providers.
  A subscription must never be assumed equivalent to paid API quota.
- Support multiple profiles/accounts/keys, health, per-account quotas,
  rate limits, cooldown, key rotation, safe provider fallback and
  request-side-effect discipline. Key rotation and provider fallback
  are distinct; never retry a side-effecting operation blindly.
- Resolve actual context window, max output, token pricing and cache/
  reasoning capabilities from authoritative provider metadata when
  available; use a maintained catalog such as models.dev as a fallback,
  with TTL/ETag caching, provenance, refresh and per-model user overrides.
  Unknown price/limits remain **unknown**, never invented or assumed free.
- Support provider-native thinking/reasoning fields, effort options,
  opaque signatures, streaming deltas, token accounting, cache reads/
  writes and round-trip serialization without dropping provider state.
  Evaluate stable libraries/SDKs against actual versions before
  introducing adapters or dependencies. Preserve raw reasoning privacy
  and compatibility constraints.

**Exit gate:** versioned provider contract tests, two real providers
and local-compatible fallback where available; audited credential
isolation, model metadata/override behavior, reasoning and cache usage.

#### M5.5 — MCP, skills, hot reload and verified learning

- Provide installation/configuration/discovery/enable-disable for MCP
  servers and skills via the shared Gateway, with owner-scoped
  permissions, explicit consent and command/slash-command integration.
- Support hot reload with versioned catalog snapshots: new turns can
  use updated capabilities without restarting the whole Gateway;
  in-flight turns retain the exact trusted schemas and permissions
  admitted at their start. Report when a refresh changes prompt-prefix
  caching or when a provider cannot defer tool loading.
- Support skill creation, extraction of reusable solutions, testing,
  provenance, inspection, versioning, rollback and opt-in publishing.
  Learning from conversations is not an unreviewed elevation of
  executable code or instructions; auto-learning must honor consent
  and treat external/skill content as untrusted.

**Exit gate:** live MCP/skill add-update-disable without lost sessions;
old-turn/new-turn snapshot security, cache behavior and tested skill
learning/revert flows.

#### M5.6 — Persistent agents, persona and memory

- Store a persistent default agent/persona in user configuration rather
  than requiring per-chat persona files. Support multiple selectable
  agents with editable `SOUL.md`, `USER.md`, `MEMORY.md`,
  `AGENTS.md` and optional identity/config files, with documented
  scopes, precedence, migration and safe updates. Context files
  inform behavior but **cannot** grant execution authority.
- Offer local lightweight memory by default and optional Hindsight
  self-hosted/cloud through an isolated retain/recall adapter. Keep
  memory database distinct from context/persona files. Support explicit
  consent, user/agent isolation, retention, forgetting, export, backup,
  migration without loss and cache-compatible context assembly.

**Exit gate:** agent/profile changes persist across restart and devices,
owner isolation and migration tests pass, and memory opt-in/out,
deletion/export and restore are verified.

#### M5.7 — Telegram as a first-class Gateway client

- Use the **same** Agent Core, session contracts, personas, models,
  permissions and tool results as Web/CLI/TUI. Telegram bot onboarding
  is optional; link numeric user identity and enforce allowlists,
  deny unknown users/groups, and isolate user/chat/thread state.
- Support commands, session selection, messages, edit/update, files,
  voice attachments where enabled, real streamed/progress messages,
  approval cards and decisions, cancellation, resumption and scheduled
  notifications without a Telegram-only agent implementation.

**Exit gate:** real owner-linked Telegram-to-Web/CLI conversation and
tool-approval continuity, denial, unknown-sender, group/thread
isolation and restart tests.

#### M5.8 — Onboarding and configuration lifecycle

- Introduce incremental Web/CLI onboarding for user/agent identity,
  language, provider/model, credential mode, memory backend, tool
  permissions, optional Telegram and a real connection test.
- Allow reconfiguration without losing conversations, models or
  preferences. Defaults remain private, maintainable and free of
  mandatory paid services; secrets are never exposed in logs or Git.
- Include consistent UX for agent selection, tools, model capabilities,
  approval policy, YOLO status and failure/rollback explanations.

**Exit gate:** fresh and existing installs complete or skip onboarding;
settings remain stable across restart and backend changes.

#### M5.9 — Workflows, integrations and autonomy

- Deliver shared workflow definitions, scheduled/conditional jobs,
  long-running resumable tasks, notifications, MCP-powered integrations
  and slash commands using the existing Gateway auth, admission and
  audit boundaries.
- Define state, idempotency, cancellation, backoff, receipts, missed-run
  behavior and side-effect recovery. Preserve user permissions; no
  silent provider spending or unbounded unattended actions.
- Keep integrations modular and optional, with secure credential
  management and backup/export for their configuration.

**Exit gate:** repeatable local/remote workflow and scheduled Telegram/
  Web notifications, denial/expiry/cancel/restart behavior and tested
  duplicate-effect prevention.

#### M5.10 — Integrated capability acceptance and closeout

- Validate real providers, native tools, approvals, YOLO, CWD, MCP,
  skills, agent identity, memory, onboarding, Telegram and workflows
  through the one Gateway, with safe default permissions and accurate
  client reporting.
- Validate desktop/mobile Web, CLI, TUI and Telegram according to
  their supported workflows. Use scoped tests during development and
  complete CI/security/release verification on the publication trigger.
- Update docs, website and release-facing feature states **only** after
  verified exit gates; explicitly label planned and preview-only work.

**M5 done when:** every M5.x exit gate passes, required first-class
capabilities operate through one authenticated Gateway, and no client
claims tool execution without a real authorized tool event.

**Tool contract references:** primary:
[Claude Code built-in tools](https://code.claude.com/docs/en/tools-reference),
[Anthropic tool-use contract](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works),
[Anthropic Bash tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/bash-tool),
and [Anthropic text editor](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool).
Compatibility references:
[Hermes built-ins](https://hermes-agent.nousresearch.com/docs/reference/tools-reference),
[Gemini CLI tools](https://geminicli.com/docs/reference/tools/),
[OpenAI Codex tool guidance](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide),
[OpenAI native apply_patch](https://developers.openai.com/api/docs/guides/tools-apply-patch),
[OpenClaw exec](https://docs.openclaw.ai/tools/exec), and
[MCP tool specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
Official version-specific documentation and runtime tests take priority
over static name lists and assumptions about what every model learned.

### M6 — Unified client experience and final parity

**Planned; this is the former M5, moved after the new M5 capability
foundation.** Capability-level parity is required throughout M5; M6
focuses on closing remaining UX and compatibility gaps rather than
building parallel engines.

- Align Web, CLI, TUI and Telegram authentication, permissions, agent
  selection, model/account profiles, CWD, approval policy, YOLO state,
  tool status, streaming/replay, errors and session recovery.
- Retire the legacy Chat/Agent UI split after supported clients migrate;
  keep versioned API compatibility and documented deprecations.
- Ensure user/agent profiles, memory settings, conversations and
  preferences remain coherent across devices and adapters.
- Validate real mobile/desktop Web, keyboard-only TUI, installed
  local/remote CLI, Telegram messages/attachments/approval workflows,
  accessibility and hostile-client authorization tests.
- Verify clients never show fabricated tool execution, stale approvals
  as executable, or conflicting durable turn results.

**Done when:** the same authorized agent/session can be accessed and
operated safely from all four clients, with consistent capabilities,
denials, recovery and UX, backed by live acceptance evidence.

### M7 — Installation, operational reliability and distribution

**Partially implemented foundation; this is the former M6, renumbered
without resetting historical release accomplishments.**
 CI-gated semantic-release published the initial [v0.1.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.1.0) and the AMD64/ARM64 GHCR image build completed successfully. The subsequent [v0.2.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.2.0) and its ARM64/AMD64 container-build and version-tag workflow jobs have completed successfully after CI. Clean-machine installation and runtime image validation remain separate acceptance work. The [release automation](deployment/RELEASE_AUTOMATION.md) and [0.1.0 notes](releases/0.1.0.md) document the foundation; clean-machine installation, runtime compatibility and restore validation remain pending.

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
