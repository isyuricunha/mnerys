# M2 execution plan — authoritative Agent Core and Gateway

**Status: M2.1 in progress, partial internal authority boundary implemented; the standalone authenticated Gateway and other M2 deliverables remain planned.** This is the technical handoff from the [M1 design exit review](M1_EXIT_REVIEW.md). The target is a **single server-owned Gateway/Agent Core workflow** shared safely by Web and future clients. The full [product roadmap](../ROADMAP.md) also defines CLI packaging (M3), TUI (M4), cross-client UX parity (M5), and installation/release reliability (M6); this plan must not mark those deliverables as shipped.

Read the [M1 source audit](AGENT_CORE_GATEWAY_M1_AUDIT.md), [domain/API contract](GATEWAY_AGENT_CONTRACT.md), [session writer inventory](SESSION_WRITER_INVENTORY.md), [revision/migration plan](SESSION_REVISION_MIGRATION.md) and [regression baseline](M1_CONTRACT_TEST_BASELINE.md) before implementation.

## Decision log

| Decision | Constraint |
| --- | --- |
| **One Python runtime** | Keep using the existing agent loop and hardened dispatcher; extract them behind a server service instead of cloning them into Web, CLI or TUI |
| **The Gateway owns authority** | Authenticate each transport and derive a trusted Principal; ownership, scopes, policies, tool approvals and effects remain server-enforced |
| **Web is the first real adapter** | Preserve current HTTP endpoints during migration; define a versioned Gateway API alongside only after explicit authorization and security regression coverage |
| **SQLite and PostgreSQL 17 are separate supported primary backends** | No assumption that production data are migrated by switching a URL; test transactions, DDL, rollback and restore on each backend |
| **Compatibility before cutover** | Preserve existing sessions, messages, preferences, tool decisions and background jobs; deploy to the private preview only after an approved rollout |
| **No false exactly-once guarantee** | Use idempotency keys and durable receipts for retried operations, with explicit reconciliation of ambiguous effects; refuse unsafe replays |

## Execution order and deliverables

### M2.1 — Finish the internal authority and turn boundary

**Goal:** one owner-scoped, transport-neutral service that controls a turn before the agent loop starts.

- Inventory the current `routes/chat_routes.py` turn admission, request authority, tool policy, model/context selection and persistence sequencing against the [existing audits](AGENT_CORE_GATEWAY_M1_AUDIT.md).
- Extract the remaining application policy and trusted request-authority construction into server-owned services. Route adapters must supply **only validated principal context and untrusted request arguments**.
- Keep the existing `src/tool_execution.py` dispatcher and sealed `src/tool_approvals.py` behavior; deny operations lacking bound authority. Preserve optional MCP, memory, model and skills integrations.
- Define concrete Python service protocols and typed errors for owner missing, forbidden, conflict, stale turn, expired cursor and unsupported capability. Keep SSE serialization in the transport layer.
- Add Web owner/bearer scope, direct/fake tool invocation, nested task inheritance and denial-path regressions.

**Incremental implementation:** the Web stream route now calls [`GatewayTurns.build_authority`](../../src/gateway_turns.py), which delegates to the [admitted authority builder](../../src/gateway_turn_authority.py) using the server-issued admission proof and server-derived internal-tool classification. The service derives owner/session from the admission instead of accepting them as independent route arguments, preserves the existing intent and `ToolPolicy` builder, rejects forged/missing admission, and adds delegated-credential deny restrictions regardless of owner privileges. [Targeted tests](../../tests/test_mnerys_gateway_turn_authority.py) cover the behavior. Exact approval **issuance** still uses the inherited flow; decision consumption now goes through the internal Gateway facade into the unchanged sealed store. Context assembly, model selection, storage and tool execution remain unchanged. This is **not** completion of M2.1.

**Second incremental slice — exact approval continuation:** the Web route authenticates the caller and forwards the already-consumed server-side object to `GatewayTurns.build_authority` for session-bound grant preparation. The [authority builder](../../src/gateway_turn_authority.py) verifies its type and owner/session/workspace binding, checks the originating authority's same bindings, refuses internal-tool and delegated-token approvals, and reapplies the current tool policy plus delegated restrictions. A missing original authority results in an **empty** grant set; approval continuation does not reinterpret the resumed message as new permission. The dispatcher remains responsible for validating the sealed operation digest and claiming the exact action once. This is not a new approval issuer, a durable approval registry or a remote decision API.

[Regression coverage](../../tests/test_mnerys_gateway_approval_authority.py) includes consumed store decisions, disabled-tool precedence, workspace normalization, forged/mismatched bindings and the no-new-grants rule. An expanded local authorization/approval run passed **499 tests**, and a separate chat/context/streaming regression passed **290 tests**. Two unrelated tests that execute Bash against the actual container isolation runtime failed in the available test environment with `containment unavailable (filesystem, process_tree)`; isolation was **not** bypassed or relaxed, and their real-execution success remains unverified here. Hosted CI for this unpushed slice is not claimed.

**Third incremental slice — approval decision validation and consumption:** the Web adapter still enforces `require_user_approval_request` and rejects delegated HTTP credentials before invoking [`GatewayTurns.decide_tool_approval`](../../src/gateway_turns.py). The new [internal decision service](../../src/gateway_approval_decisions.py) independently rejects fabricated admission, delegated/internal callers, invalid decision strings and plan mode; it checks the pending record's owner/session before destructive consumption through the **same** [`ToolApprovalStore`](../../src/tool_approvals.py). The result contains only server-side pending/exact records and existing untrusted-context taint. On the Web side, HTTP errors retain their previous 400/403/409 classifications; legacy history-card resolution, deny SSE response and sealed continuation-context restoration remain unchanged. The inherited wire values deliberately map `approve` to **chat-session** scope and `approve_task` to **task** scope; no new permission widening was introduced.

[Regression tests](../../tests/test_mnerys_gateway_approval_decisions.py) cover valid/invalid decisions, token and internal request rejection, owner/session isolation, expiry, replay, plan-mode veto, taint propagation, single-user mode and a disappearing pending grant. The server-side approval store is still **process-local**, not durable or cross-worker; tool issuance, external effects and the actual dispatcher remain on inherited paths. This is **partial M2.1 implementation**, not a public API or M2 completion.

**Fourth incremental slice — model selection and fallback context:** both existing Chat endpoints now call [`GatewayTurns.resolve_foreground_policy` and `build_foreground_routes`](../../src/gateway_turns.py) with the issued session admission, rather than resolving owner-scoped preferences and candidate endpoints in the Web adapter. The [foreground route service](../../src/gateway_foreground_routes.py) derives owner identity from admission, preserves the inherited per-owner model allowlist/fallback policy and selected endpoint identity checks, and rejects forged, foreign-session or cross-owner policy reuse. It returns candidate tuples and **safe descriptors in the same order**; provider headers remain in candidate tuples, not user-visible route metadata. No model provider or fallback behavior was replaced.

When fallback is enabled, the [context factory](../../src/gateway_foreground_context.py) now receives the same admitted session and policy before shaping a **route-neutral copy of the original messages per candidate**. It calls the existing compactor with `persist=False`, carries per-candidate context limits, summaries and trim metrics, and rejects a different session object. The inherited Web turn still decides which completed route actually answered and applies **only that route's** prepared compaction. Strict-mode turns continue on the existing non-fallback path. Existing memory retrieval, prompt construction, runtime/provider dispatch, foreground event rendering and persistence remain in their original components.

[Foreground model regression tests](../../tests/test_mnerys_gateway_foreground_routes.py) and [context boundary tests](../../tests/test_mnerys_gateway_foreground_context.py), together with the original [fallback integration tests](../../tests/test_foreground_model_routing.py), cover owner allowlists, bearer/single-user principals, cross-session denial, route metadata/endpoint identity, different context windows, source-history preservation and no early compaction write. Local regressions passed **380 model/context/security tests** and **288 turn/streaming/session/tool tests**. The only shared warning was the existing pytest-asyncio fixture-scope configuration warning; hosted CI for this unpushed change has not run. This work does **not** establish model selection through an independent external Gateway client or complete M2.1.

**Fifth incremental slice — final prompt handoff:** the Web context builder still owns presets, attachment processing, memory/RAG, message persistence, and platform-specific preprocessing. After it returns, both Chat routes now call [`GatewayTurns.append_internal_context`](../../src/gateway_turns.py) to guard any server-classified internal-tool content and avoid introducing fresh user consent. The internal [prompt preparation service](../../src/gateway_prompt_preparation.py) requires the issued foreground policy and matching admitted owner, appends untrusted tool data to both history representations without duplicate entries, and delegates final strict-versus-route-neutral prompt selection to [`GatewayTurns.select_prompt_messages`](../../src/gateway_turns.py). For detached streams, the inherited current-request consistency check is retained; exact approval continuations keep the original transcript **without appending unrelated composer text**. Diagnostic warnings no longer log excerpts of potentially private conversation content.

This is a *bounded handoff extraction*, **not** a claim that the complete `build_chat_context` pipeline or provider invocation is transport independent. The subsequent work must separate request-specific adapters, context injection, storage and post-response processing before the full turn is owned by the Gateway. [Focused tests](../../tests/test_mnerys_gateway_prompt_preparation.py) exercise forged/foreign admission, context-owner mismatches, strict and fallback histories, multimedia messages, prompt-guard delimiter escaping, single-user mode, and approval continuation. Local regression results: **203 targeted tests** and **508 broader integration tests** passed, with only the existing pytest-asyncio fixture-scope warning. These suites overlap; their totals must not be interpreted as 711 unique tests. Hosted CI and preview deployment have not been run for this local slice.

**Sixth incremental slice — HTTP-independent shared context builder:** the inherited [`build_chat_context`](../../routes/chat_helpers.py) now acts as an HTTP adapter that resolves the trusted request principal, preserves the original message-event callback and forwards all existing options into `build_chat_context_core`. The shared implementation accepts an explicit server-side owner and optional notification callback; **it never reads an HTTP `Request` or resolves identity from headers/cookies**. Both real Web endpoints now provide their issued `AdmittedSession`, and the [context admission check](../../src/gateway_context_admission.py) validates the session ID, loaded owner when available, and request principal before any preprocessing, history mutation, memory/RAG lookup or message event. The legacy no-admission helper entry point remains for compatibility with existing internal callers.

The original preset, preprocessing, memory/RAG, attachment, compact/trim and persistence integrations continue to run in their original order and module. This is a **request-dependency seam**, not a fully transport-independent context service or a new Gateway endpoint: the context algorithm still lives in `routes/chat_helpers.py`, legacy event publication still occurs in the HTTP adapter, and unrelated services still depend on process-local database and Web components. The next extraction must introduce explicit service ports and move ownership of these integrations without duplicating them or changing persistence.

[Focused admitted-context tests](../../tests/test_mnerys_gateway_context_assembly.py) exercise real issued and forged grants, mismatched HTTP principal/session, cross-owner cached session objects, pre-side-effect rejection, named/single-user compatibility, direct no-HTTP core use, preserved owner-scoped preferences/compaction, and event/persistence suppression for internal continuations. The complete local [context and gateway regression](../../tests/test_chat_helpers.py) passed **787 tests**, with one known pytest-asyncio fixture-scope warning. No hosted CI or private preview rollout has been performed for this local slice.

**Seventh incremental slice — context dependency ports:** the common context algorithm now consumes the internal [ChatContextPorts protocol](M2_CONTEXT_SERVICE_PORTS.md), with a [legacy adapter](../../routes/chat_helpers.py) that delegates to existing handlers and stores. This exposes separate service operations for presets and attachment preprocessing, owner preferences, upload manifests, memory/RAG/Web prefaces, used-memory tracking, persisted or incognito messages and compaction/trim. **There is only one live implementation**; the new interface makes existing effects replaceable in isolated tests, not independently durable or generally available over the network. The original message/event ordering, context filters and safety checks remain unchanged.

[Port tests](../../tests/test_mnerys_gateway_context_ports.py) inject a fully in-memory adapter, explicitly reject unexpected legacy service calls and confirm persisted-message ordering, incognito isolation and no write/event on internal continuation. Together with the existing Gateway/Chat/context/permission regression, **790 local tests passed** (one known pytest-asyncio configuration warning). Other context helpers remain module-bound; the next slices should isolate model normalization and remaining storage/time/source dependencies before moving the algorithm into an independent Core package. Hosted CI and live preview verification have **not** run for this local change.

**Eighth incremental slice — remaining model/time/source/history ports:** the same [context service protocol](M2_CONTEXT_SERVICE_PORTS.md) now provides six additional trusted operations: cached-first model ID normalization, YouTube transcript source attribution, persisted session-history reads, user-local clock-context messages, model context-window sizing and token estimates. The [legacy adapter](../../routes/chat_helpers.py) preserves the original implementations and side-effect order. In particular, cached model IDs still take priority over live `/models` resolution; the request-local clock remains in a user-role message next to the latest user turn to retain stable KV-cache prefixes; incognito context never uses the persisted-history reader; and deferred fallback shaping does not start compaction prematurely.

[Injected-provider tests](../../tests/test_mnerys_gateway_context_ports.py) now verify that the core uses these ports rather than direct access to the original source, clock, normalization and history helpers. They also check provider-selected model fallback, deterministic time injection, model-window selection and source provenance. **This is still a partial M2.1 dependency extraction**, not a fully independent Agent Core: pure prompt utilities, Web-specific data types and process-local state remain coupled to the legacy module. No provider configuration, persistence migration or external API was changed.

**Ninth incremental slice — transport-independent context composition:** the
shared [context algorithm](../../src/gateway_chat_context.py), its
[context result types](../../src/gateway_chat_context_types.py) and its
[pure decision rules](../../src/gateway_chat_context_rules.py) have moved from
the Web helper into the application layer. The existing Web route retains a
compatible `build_chat_context_core` wrapper that injects the inherited
`LegacyChatContextPorts` when no explicit provider is supplied. The new
`assemble_chat_context` entry point requires trusted ports and has no
dependency on FastAPI or `routes/`. This prevents an independent caller
from implicitly loading the Web adapter or inheriting a second authorization
path. The order of message persistence, preface building, incognito isolation,
context compaction and model fallback shaping remains unchanged.

This is **not** an operational Gateway, an independent agent engine or a
database migration. Admission must still be performed by the trusted caller;
the only production port implementation still wraps inherited services.
The [regressions](../../tests/test_mnerys_gateway_context_ports.py) include
direct execution without HTTP, rejection of missing ports, compatibility
types and a structural guard against imports from `routes/` or FastAPI.
Preview rollout, durable turn records and second-client acceptance are
still required for M2 completion.

**Tenth incremental slice — admitted stream delivery boundary:** the Web
stream route now calls [GatewayTurns.open_stream](../../src/gateway_turns.py)
for both detached interactive execution and direct response-bound streaming.
The [internal delivery service](../../src/gateway_stream_delivery.py) checks
the issued session admission **before either lifecycle** and preserves the
existing decision: regular interactive chat/agent runs remain detached and
resumable, while Compare panes and explicitly unattended native runs close on
client disconnect. The Web adapter still writes the existing SSE response and
the legacy run-ID header only for detached runs. This is not a new event store,
authorization engine or public transport API; the detached branch continues to
use the original [TurnService](../../src/turn_service.py) and in-memory run
manager. Client runtime hints only affect disconnect behavior, never session or
tool permissions.

[Dedicated Gateway delivery tests](../../tests/test_mnerys_gateway_stream_delivery.py)
cover forged and modified admissions, browser/bearer sessions, exact-run
resume/cancellation, unchanged direct-stream behavior, and structural
separation from FastAPI. [Compare disconnect tests](../../tests/test_compare_stop_disconnect_poll.py)
and [fallback/resume regressions](../../tests/test_live_fallback_round_attribution.py)
verify the caller and client-facing lifecycle. The local focused suite passed
**99 tests**; a separate routing, context, authority, and security suite passed
**120 tests**. They are independent groups, not 219 end-to-end client tests.
Hosted CI, restart durability, two-client acceptance and private preview
rollout remain unverified at this slice.

**CI regression maintenance after the extraction:** the sharded GitHub Actions
run for commit `28ccf5bf` exposed three outdated test assumptions:
[model-send history hydration](../../tests/test_history_display_model_hydration.py)
mocked session admission as `None` despite the now-mandatory issued
`AdmittedSession`; the
[cached-model test](../../tests/test_chat_cached_model_normalization.py)
searched for model-normalization code in its pre-extraction Web location; and
[owner-scoped compaction](../../tests/test_aux_llm_owner_scope.py) inspected the
Web helper even though the compaction invocation moved into the shared Core.
The updated hydration fixture issues an owner-matched admission using the
real in-process policy, model tests assert the live cache-first behavior of
`LegacyChatContextPorts` and basename matching, and the compaction test
examines the actual Core call's owner argument. These test fixes do not
weaken runtime admission, modify model selection or affect persistence.
Local focused and authority regressions passed **63** and **96** tests,
respectively; complete hosted CI remains the authoritative merge/release
gate.

**Eleventh incremental slice — owner-bound post-response dispatch:** the
[Gateway facade](../../src/gateway_turns.py) now mediates the existing
post-response processing for synchronous Chat and both streaming paths.
Its [transport-independent dispatch service](../../src/gateway_post_response.py)
checks the issued session admission, loaded session ID and owner, and
context-result owner **before** calling the injected legacy task runner.
The owner and session passed to that runner come only from the admission,
not mutable request fields. The service retains the existing extraction
eligibility policy (tool denial, exact approval continuation and unattended
mode) and rejects malformed server flags. Client runtime hints may suppress
auxiliary LLM extraction, but cannot grant new permissions.

The original [post-response implementation](../../routes/chat_helpers.py)
continues to own sequential memory/skill extraction, token accounting,
webhooks and auto-naming. It still runs on the same process-local queue:
this slice does **not** introduce durable jobs, restart recovery, a new
scheduler, a new public endpoint or a second memory provider. The
[admitted dispatch tests](../../tests/test_mnerys_gateway_post_response.py)
cover forged/tampered admission, cross-owner cached sessions, denied side
effects, normal versus unattended extraction and the three Web handoffs.
Local regression groups passed **103**, **130** and **182** tests with the
existing pytest-asyncio/SQLAlchemy warnings (group overlap possible).
The full hosted CI and preview have not been verified for this local slice.

**Twelfth incremental slice — admission-bound final tool policy:** the
[existing Gateway execution policy service](../../src/gateway_execution_policy.py)
now composes the final server-side tool denylist **after** the inherited Web
request-intent, web-search, incognito and email constraints. It requires an
issued session admission, the matching loaded session and the context-builder
owner; all comparisons fail before a tool policy is returned. The
[GatewayTurns facade](../../src/gateway_turns.py) consumes trusted
account privileges, global disabled-tool settings and browser-tool inventory
from the Web adapter. It preserves the original precedence of delegated-token
denials, account restrictions, global restrictions, light personal-tool
auto-escalation, Compare session limits, Compare Chat restrictions, Plan
read-only limits and guide-only tool suppression. An account with no Agent
privilege is demoted **before** Compare's chat-only clamp. Legacy callers
without an explicit mode remain accepted; Compare treats non-Agent modes
conservatively.

[Focused policy tests](../../tests/test_mnerys_gateway_final_tool_policy.py)
cover forged, tampered and foreign-owner session admission; bearer and
browser privileges; global denials; workspace versus personal auto-escalation;
Compare/Plan/guide-only behavior; legacy empty-mode routing; and the real
Web handoff. [Existing route regression tests](../../tests/test_chat_route_tool_policy.py)
now check the new service boundary rather than the old source-code location.
This is still **partial M2.1**: intent parsing, model execution, tool
contract/schema selection, runtime tool dispatch and persistence remain in
their inherited implementations. No public API, scheduler, database schema,
preview deployment or second tool-policy engine was introduced. Local tests
passed **133 focused**, **245 streaming/model** and **114 context/tool**
cases; suites may overlap. Hosted CI was not run for this local slice.

**Thirteenth incremental slice — admitted runtime and contract eligibility:**
the existing Web stream adapter now calls two [GatewayTurns service
methods](../../src/gateway_turns.py) rather than making early compact
runtime and late tool-contract eligibility decisions inside the HTTP route.
The [transport-independent selection service](../../src/gateway_turn_contract_selection.py)
validates the issued session proof, loaded session identity and server-derived
owner for both handoffs. It preserves the pre-context compact-runtime decision
(including full-schema, TUI, approval-continuation, Agent privilege and image
generation exclusions) and keeps that decision separate from the later native
workspace rule. The latter still requires the native runtime surface plus
the terminal-agent marker and either server-resolved workspace intent or an
unattended, bound workspace; a client surface marker alone grants nothing.
The original `_turn_contract_enabled` function remains available as an
import-compatible Web alias.

The immutable [turn-contract resolver](../../src/turn_contract.py), schema
inventory, compact preview scope, policy filtering and
[tool execution dispatcher](../../src/tool_execution.py) are **unchanged**
and still used once by the inherited agent runtime. This slice is not an
independent Gateway transport, a new model executor, or a durable turn store.
[New tests](../../tests/test_mnerys_gateway_turn_contract_selection.py)
cover forged/tampered admission, cross-owner sessions, browser/bearer and
single-user projections, client-marker limits, legacy selection rules,
direct Gateway service use and both Web handoffs. The original
[Web policy checks](../../tests/test_chat_route_tool_policy.py) now assert the
new location of native-workspace eligibility without removing the guard.
Local tests passed **177 focused**, **185 routing/streaming** and
**159 authority/approval** checks (suite overlap possible); full hosted
CI and preview deployment remain unverified for this local commit.

**Fourteenth incremental slice — admitted immutable routed-contract handoff:**
the Web adapter now calls [GatewayTurns.resolve_routed_contract](../../src/gateway_turns.py)
instead of invoking the immutable resolver directly. The
[transport-neutral handoff](../../src/gateway_routed_contract.py) checks the
issued admission, loaded session, assembled context owner and existing
[RequestAuthority](../../src/agent_runtime/authority.py) owner/session binding
**before** invoking the same
[resolve_turn_contract](../../src/turn_contract.py) function. It returns the
unchanged immutable `TurnContract` and the original authority restricted by
the resolved `ToolPolicy`. Tool schemas, prior successful tools and selected
capabilities remain *selection inputs only*; this service cannot create
operation grants or change the existing dispatcher.

The legacy route still prepares the trusted schema inventory and owner-scoped
restrictions, selects exact tools, applies compact preview/experiment scope,
and reconciles final disabled names for the runtime. Those stages are **not**
yet independent Gateway services. Model invocation, tool execution and
persistence remain on the inherited implementation; no new resolver, public
API, database migration or preview deployment was introduced.
[Negative and parity tests](../../tests/test_mnerys_gateway_routed_contract.py)
cover browser, bearer and single-user admissions; forged, tampered and
cross-owner sessions and authorities; guide-only and disabled tools; exact
selected/required/warm arguments; and the single Web handoff. Local
regressions passed **200 focused**, **185 routing/streaming**,
**159 authority/approval** and **1,129 core contract compatibility**
cases (groups may overlap). Hosted CI and preview verification remain
pending for this local change.

**Fifteenth incremental slice — admitted compact scope and final tool reconciliation:**
the [Gateway contract-scoping service](../../src/gateway_contract_scoping.py)
now validates the issued admission, loaded owner/session, original
[admitted routed contract](../../src/gateway_routed_contract.py) and bound
request authority before applying the inherited compact preview/experiment
selection and final denylist. The [GatewayTurns facade](../../src/gateway_turns.py)
receives server-resolved intent, schema inventory, policy, runtime flags and
the untrusted experiment header. The service resolves any experiment mode
using **only the admitted owner and server-loaded model**; the header itself
cannot select a fixture/experimental mode for an unauthorized owner.

The service calls the unchanged
[scope_preview_contract](../../src/clean_agent_preview.py),
[select_experiment_inventory](../../src/tool_routing_experiment.py),
[resolve_full_inventory_contract](../../src/turn_contract.py) and
[build_effective_tool_policy](../../src/tool_policy.py). It preserves the
compact/native extra-tool rules, warm tool history, structured-reader
preference and explicit media-to-artifact chains. The final contract is
still filtered by the server's tool policy; the service reconciles the
original runtime denylist against that contract without creating operation
grants, modifying provider selection or bypassing the hardened dispatcher.
The [Web adapter](../../routes/chat_routes.py) now supplies these values
through a single admitted handoff instead of owning the ~100-line scope
block. Schema collection and intent classification remain inherited.
The finalizer additionally refuses a mismatched internal policy/authority
pair (missing deny flags, guide-only or MCP restriction) or any already-offered
tool that violates the trusted policy, before scoping. This is a monotonic
defense check and does not alter valid routed contracts.

[Scoping parity and denial tests](../../tests/test_mnerys_gateway_contract_scoping.py)
compare the new service against the pre-extraction Web behavior for ordinary,
compact, native, OCR, tasks, transcriptions, tool warm-state and experiment
cases; also cover tampered/foreign admission, mismatched authority, malformed
trusted flags, and no experimental scope from an unauthorized header. The
[previous routed-contract tests](../../tests/test_mnerys_gateway_routed_contract.py)
now check this new handoff rather than a source-text implementation detail.
Local suites passed **242 focused**, **185 routing/streaming**,
**159 authority/approval**, **1,129 contract compatibility** and
**160 routing experiment/product** tests (some groups overlap). No public transport, additional model/tool executor,
durable event/job store, database change or preview deployment. Hosted CI
remains pending for this local change.

**Sixteenth incremental slice — admitted native/MCP tool inventory:**
the [Gateway facade](../../src/gateway_turns.py) now delegates native
and MCP schema inventory assembly to an
[owner-admitted service](../../src/gateway_tool_inventory.py). Before any
schema or MCP manager lookup, the service verifies the issued admission,
loaded session and context owner, as well as server-resolved input types.
It reuses the inherited native schemas, MCP manager/disabled map,
owner-blocked list, delegated-credential restrictions, SFT Web filesystem
guard and [tool policy builder](../../src/tool_policy.py). The environment
`host_shell` bridge remains absent from the Web-facing native inventory.
An explicit no-tools request still disables all available schemas, and an
already-denied policy may never be widened by inventory reconstruction.

The [Web compatibility route](../../routes/chat_routes.py) now receives
the returned schema tuple, owner-blocked tools, disabled snapshot and
effective policy from the Gateway. Its old no-tools classifier remains
importable as a compatibility alias. **Request-specific selected-tool
classification, contextual follow-ups, fixture family restoration and
intent-derived capability routing are still Web-owned**, as are the
inherited agent loop and hardened dispatcher. This service does not
mint execution grants, reimplement MCP, introduce a second tool engine,
create a public Gateway API, or change DB schema or preview state.

[New isolated inventory tests](../../tests/test_mnerys_gateway_tool_inventory.py)
compare the inherited ordering under native/MCP, SFT/native workspace,
bearer, owner-blocked, Plan, guide-only, and globally disabled cases.
They deny fabricated admission, foreign owner/session and malformed
trusted flags before inventory access. Local suites passed **282 focused**,
**185 routing/streaming**, **159 authority/approval**, **160 peripheral
product/experiment**, **1,129 contract compatibility**, and **148 external
schema/API-token/file-edit** tests, with some overlap. Hosted CI and live
preview behavior have not yet been verified for this local commit.

**Seventeenth incremental slice — admitted exact tool selection and typed follow-ups:**
the Web adapter now passes the admitted session and existing
[Gateway tool inventory](../../src/gateway_tool_inventory.py) to
[GatewayTurns.select_tool_intent](../../src/gateway_turns.py), backed by the
[transport-neutral intent service](../../src/gateway_tool_intent.py).
The service validates the original admission, loaded session and context owner
**before** classifying any request or invoking the inherited, server-sourced
recent-web-tool lookup. It preserves the exact selection precedence of
[the original request classifier](../../src/turn_contract.py), typed
browser/Web follow-ups, visible-editor write helpers, fixture-only personal
capability restoration, independent-source fetch preference, open-email
`ui_control` requirements and corrected-browser recovery. The fixture
exception still subtracts account/global denials and owner-blocked tools;
Web retains its compatibility audit logging.

The request-specific selected and required names are **schema selection
hints, not execution grants**. The original immutable routed contract,
owner-scoped tool inventory, final denylist and
[hardened tool dispatcher](../../src/tool_execution.py) remain in force.
The Web route continues to determine the earlier request capability families,
workspace/browser state and typed history evidence and still invokes the
inherited agent/model loop; no second classifier, remote Gateway API,
database change or preview deployment was introduced.

[New parity/denial tests](../../tests/test_mnerys_gateway_tool_intent.py)
compare original ordering for empty versus unspecified selections, browser
and Web continuations, repeated search versus source fetch, editor,
email, fixture owners and corrected target. They also verify that invalid
admissions stop classifier/history access and that malformed flags and
invalid prior-tool evidence fail closed. The
[fixture-policy source regression](../../tests/test_review_fixture_policy.py)
now asserts the rule at its new location. Local checks passed
**329 focused**, **185 routing/streaming**, **159 authority/approval**,
**160 peripheral**, **1,129 contract compatibility** and **148 external
schema/API-token/file-edit** tests (test suites may overlap).
Hosted CI and real preview validation are still pending for these local commits.

**Eighteenth incremental slice — admitted initial capability classification:**
the Web adapter now delegates its initial request-family classification to
[GatewayTurns.classify_turn_capabilities](../../src/gateway_turns.py), backed by
[one transport-neutral service](../../src/gateway_turn_capabilities.py). The
service verifies the issued session admission, loaded owner and context owner
**before** invoking the original
[requested_capabilities and corrected_browser_target classifiers](../../src/turn_contract.py)
with server-loaded session history. It preserves explicit browser/discovery
intent, typed warm-browser contextual inheritance only when the original
classifier found no family, and a corrected browser URL even when tool
contracts are disabled. It returns distinct **active capabilities** required
by routing and **offered capabilities** after adding a server-owner-checked
visible document; that document never becomes a required mutation merely
because its editor is open. The original Web suppression of unrelated
explicit-web intent and pre-augmentation image-edit classification also
remain intact.

The Web route continues to verify active-document ownership, derive
workspace and browser routing facts, and inject image-edit attachment
instructions into its context. The same earlier account/mode policy, exact
tool selection, immutable contract resolution, compact scoping and hardened
tool dispatcher remain in effect. This is an in-process authority seam, **not**
a new classifier, tool engine, remote Gateway API or model executor. No
database, persisted session or running preview was modified.

[New parity and denial tests](../../tests/test_mnerys_gateway_turn_capabilities.py)
compare the inherited route rules across document, workspace, image,
browser/discovery, typed history and no-contract branches; assert that warm
browser evidence cannot override an unrelated family; check bearer,
browser and single-user admissions and deny forged/mismatched sessions or
malformed trusted flags before classification. The
[existing browser-capability source guard](../../tests/test_chat_route_tool_policy.py)
now verifies its relocated Gateway boundary. Local suites passed
**379 focused**, **185 routing/streaming**, **159 authority/approval**,
**160 peripheral**, **1,129 contract compatibility** and
**148 external schema/API-token/file-edit** tests (overlap is possible).
Hosted CI and preview acceptance checks remain pending for these local commits.

**Nineteenth incremental slice — admitted Web intent reconciliation and opt-in gates:**
the inherited Web route now delegates its **post-capability public-Web intent
reconciliation and initial tool-denial ordering** through
[GatewayTurns.reconcile_web_intent](../../src/gateway_turns.py) to a
[session-admitted service](../../src/gateway_web_intent.py). The service checks
the issued admission, loaded session and context owner **before reading
server-provided Web history evidence**. It retains the inherited distinction
between private prior-chat searches and public search, explicit request
classification, typed contextual link/result follow-ups, the trained compact
route, opt-in Web toggles and explicit request-level search denials. Browser
automation and local HTML rendering retain separate gates, and exact public
URL reads do not imply permission for open-ended search.

The same implementation preserves pre-final-policy legacy Web containment:
non-contract public Web requests cannot drift into unrelated personal,
document, messaging or shell tools except for the inherited bound
workspace-output case. A later [final admitted tool policy](../../src/gateway_execution_policy.py)
still reapplies account, delegated-token and global restrictions. The
Web adapter continues to detect initial URL/browser/workspace intent, decide
Chat-to-Agent escalation and collect server-loaded history evidence via
**lazy, validated callbacks**. These Web-specific compatibilities are **not**
yet a standalone model/agent orchestration service. No new classifier,
tool dispatcher, API, DB migration or preview deployment was introduced.

[Independent parity tests](../../tests/test_mnerys_gateway_web_intent.py)
compare the previous Web ordering across explicit toggles, user "no search"
instructions, prior-chat requests, typed contextual evidence, compact
routing, exact URLs, browser/local rendering and workspace outputs. Negative
tests reject forged or cross-owner sessions, malformed server flags and
invalid evidence callbacks **before** performing external lookups.
Legacy [Web source-policy](../../tests/test_chat_route_tool_policy.py) and
[final policy boundary](../../tests/test_mnerys_gateway_final_tool_policy.py)
guards now verify the relocated Gateway implementation. Local suites passed
**452 focused**, **185 routing/streaming**, **159 authority/approval**,
**160 peripheral**, **1,129 contract compatibility** and **148 external
schema/API-token/file-edit** cases (some groups overlap). Hosted CI and live
preview acceptance remain pending for these unpushed local commits.

**Twentieth incremental slice — pure early Chat-to-Agent routing preview:**
the [Web compatibility adapter](../../routes/chat_routes.py) now uses
[GatewayTurns.preview_initial_agent_route](../../src/gateway_turns.py)
and a [pure initial routing service](../../src/gateway_initial_agent_routing.py)
for the pre-session chat-to-agent decision. The service reuses the single
[legacy action-intent classifier](../../src/action_intents.py), preserving
the original precedence of typed tool action, opted-in search, explicit
public-Web request and explicit private-browser workflow. It preserves
the independent native workspace-intent hint, including the inherited
`allow_bash="true"` *preliminary toggle* only for an automatically
promoted workspace action; existing Agent mode retains its previous value.
The Web route retains the unchanged trained-compact candidate hint and
audit messages. The result is an immutable, side-effect-free value,
not a tool permission, admission or operation grant.

**Trust boundary:** this initial step must run **before the default/submitted
session can be resolved and admitted**. It therefore deliberately does
not claim a session owner or call protected resources. The later
[issued admission](../../src/gateway_admission.py), privilege gates,
[execution-policy finalizer](../../src/gateway_execution_policy.py) and
[hardened dispatcher](../../src/tool_execution.py) remain mandatory for
workspace access, shell execution and all other effects. After admission,
the inherited route still reconciles its loaded model, typed contextual
browser/Web follow-ups, explicit workspace path, visible document editing
and turn authority. Those subsequent branches have **not** been extracted
by this slice. This is not a public API, independent agent runtime,
durable Gateway or database migration.

[Preflight precedence and safety tests](../../tests/test_mnerys_gateway_initial_agent_routing.py)
compare the inherited source logic for chat/agent modes, typed notes/calendar/
shell actions, Web/browser priorities, opaque or empty messages, native
workspace hints and the no-new-grant guarantee. The
[workspace routing regression](../../tests/test_chat_route_tool_policy.py)
now checks the rule's new source location. Local suites passed
**491 focused**, **185 routing/streaming**, **159 authority/approval**,
**160 peripheral**, **1,129 contract compatibility**,
**148 external schema/API-token/file-edit** and **56 original action-intent
plus new preflight** cases (overlap is possible). Hosted CI and real preview
acceptance are still pending for this local implementation until publication
and an authorized deployment.

**Post-publication CI regression repair (tenth local-commit batch):**
the first push of the pre-admission and Web-intent extractions triggered
three GitHub pytest failures, all from tests targeting previous source
locations. The [session hydration test](../../tests/test_history_display_model_hydration.py)
now stubs the actual classifier in
[the initial Gateway routing service](../../src/gateway_initial_agent_routing.py),
not a removed Web-route alias. The
[workspace-artifact regression](../../tests/test_workspace_artifact_tool_floor.py)
now checks the relocated
[Gateway Web-intent service](../../src/gateway_web_intent.py) and verifies
that native writers are preserved for a bound Web+workspace deliverable,
but denied for an unbound or ordinary Web request. Production code and
execution permissions were not changed.

All three previously failing tests were reproduced and subsequently passed
in the project test environment; **494 focused tests** also passed. An expanded
local subset passed **256**, skipped **1** and excluded **3** unrelated tests
that require functional process/filesystem containment unavailable in the
isolated local runner. Those three tests remain enabled and must run in
GitHub CI's explicit bubblewrap setup; local bypass is not accepted as proof.
Full hosted CI must be checked separately after the corrective push.

**Twenty-first incremental slice — admitted post-session and document mode routing:**
the inherited Web route now delegates session-dependent Chat-to-Agent
promotion through [GatewayTurns.reconcile_post_session_route and
promote_document_edit_route](../../src/gateway_turns.py) to one
[owner-admitted routing service](../../src/gateway_post_session_routing.py).
The service verifies the issued admission, loaded session and context owner
**before accessing trusted history or workspace callbacks**. It preserves the
original ordering and short-circuit behavior for a persisted compact-model
route, its successful private-browser history, referential public-Web lookup,
typed browser/form continuation and the inherited explicit workspace-path
auto-escalation. That path is still resolved and confined by the *existing Web
server-side path resolver*, never by a client-provided trust flag. The
server-resolved intent, workspace binding and legacy `allow_bash="true"`
compatibility toggle remain **routing hints only**, not execution grants.

After the Web adapter has resolved and owner/session-checked the visible
document, the same Gateway boundary also applies the inherited edit-intent
grammar to choose Agent mode for a document revision. The adapter retains
document database lookup, scope checks, optional session rebinding and
audit logging; no document permission or write is minted by this route.
The original account/global final policy, immutable execution contract and
hardened dispatcher remain authoritative. Initial URL/browser classification,
request workspace resolution, full model/agent invocation, persistence and
SSE rendering remain inherited. The standalone authenticated Gateway,
durable events and CLI/TUI adapters are still planned.

[New equivalence and denial tests](../../tests/test_mnerys_gateway_post_session_routing.py)
cover ordering, optional callback laziness, explicit and contextual Web/browser
follow-ups, workspace presence and restoration, document editing, and
bearer/browser/single-user admissions. Cross-owner/forged sessions and
malformed evidence are rejected before callback execution. The local
focused regression group now includes the two previous CI failure areas for
session hydration and
workspace writer containment, plus visible-document context. Tests passed **577 focused**, **185 routing**,
**159 authority**, **160 peripheral**, **1,129 contract-compatibility** and
**148 external-schema/API-token/edit** cases (suites overlap).
Hosted CI and live preview behavior have not been verified for this local
commit; no DB or deploy change is included.

**Twenty-second incremental slice — pure early public-Web/URL/browser intent classification:**
the Web compatibility adapter now delegates lexical public-Web, direct public
URL, interactive browser, private-browser, authoritative-source discovery and
personal-store search intent to
[GatewayTurns.preview_initial_web_intent](../../src/gateway_turns.py),
backed by the [pure, pre-admission classifier](../../src/gateway_initial_web_intent.py).
It preserves the original source regexes, ordering and private-browser
precedence, including the rule that a concrete public URL authorizes only
the original exact-reader *candidate*, not unrestricted Web discovery.
The original lexical helper names remain import-compatible reexports from
Web; there is a single source of truth instead of duplicate classifiers.

This step runs before owner/session admission and **does not** read history,
mint policy grants, invoke network tools or inspect arbitrary workspace
paths. The existing Web adapter still calculates the per-turn search toggle,
resolves and confines any workspace, and lazily supplies the local HTML
rendering predicate only for a string request with a bound workspace.
Local rendering remains separate from public-Web search and is subject to
the later admitted browser/account/global policies and hardened dispatcher.
The already-admitted post-session routing, request authority, model loop,
durable persistence and SSE transport are unchanged; the complete Gateway
and independently authenticated remote clients remain planned.

[New parity and negative tests](../../tests/test_mnerys_gateway_initial_web_intent.py)
compare the previous route's classification order for personal content,
URLs/PDF/YouTube, explicit navigation, unsubscribe/browser requests,
external discovery, optional Web search and workspace-local rendering.
They pin lazy server evidence, deny malformed internal flags and check
that the original Web helper names alias the same pure functions.
The [existing route source regressions](../../tests/test_chat_route_tool_policy.py)
now check the new service location without relaxing policy assertions.
Local checks passed **627 focused**, **185 routing**, **159 authority**,
**160 peripheral**, **1,129 contract compatibility** and
**148 external-schema/token/edit** tests (suites may overlap). Hosted CI
and the private preview have not been run for this local commit.

**Twenty-third incremental slice — admitted effective runtime toggle projection:**
the Web adapter now passes the issued session, the bound context owner,
validated runtime metadata and the **final** tool policy to
[GatewayTurns.project_runtime_ui_state](../../src/gateway_turns.py), backed by
[the transport-neutral projection service](../../src/gateway_turn_runtime_state.py).
This occurs after the final account/delegated/global denylist is applied and
before [request authority construction](../../src/gateway_turn_authority.py).
The service verifies the issued admission and loaded session/owner, then
overwrites any untrusted client `web_ui_state` values with server-computed
toggle facts. The effective `web`, `bash` and `document_editor` availability
reflects the post-policy disabled tool set; `rag`, `research` and
`incognito` retain their existing request-toggle semantics. Neither the
projection nor a client-sent toggle becomes an executable grant.

The inherited runtime context retains all other validated client metadata
without in-place mutation. The original Agent Core and
`ui_control.get_toggles` consumers continue to receive the same keys and
booleans. The Web adapter still parses client runtime context and forms;
model invocation, ownership checks, durable session handling and SSE remain
unchanged. No extra RPC endpoint, model executor, database migration or
preview deployment was introduced. This is a smaller, testable Gateway
authority/context seam, **not** a completed end-to-end turn boundary.

[Independent parity and denial tests](../../tests/test_mnerys_gateway_turn_runtime_state.py)
compare the old Web projection across policy disables, document editor state,
request modes, malformed metadata and client spoofing; verify browser, bearer
and single-user credentials; and reject forged or cross-owner admissions.
They also pin the handoff ordering: **final policy → trusted runtime toggle
projection → request authority**. Local suites passed **672 focused**,
**185 routing/streaming**, **159 authority**, **160 peripheral**,
**1,129 contract compatibility** and **148 external schema/token/edit**
cases (suites may overlap). Hosted CI and private preview acceptance
remain pending until a permitted publication and deployment.

**Twenty-fourth incremental slice — first executable Gateway-bound Agent turn:**
the inherited Web stream now enters the Agent Loop through
[GatewayTurns.stream_admitted_agent](../../src/gateway_turns.py), backed by
[the owner-admitted execution service](../../src/gateway_agent_execution.py).
That boundary checks the issued admission, loaded session and context owner,
then requires the selected model/endpoint and RequestAuthority to match the
same session, owner and workspace. Session identity, provider headers,
delegated-credential status and policy-derived denials come from trusted
server state; callers cannot override those fields through generic runtime
options. The service only **narrows** effective tool denials and forwards
the turn into the **same** [TurnService and inherited Agent Loop](../../src/turn_service.py).
It does not create a new model engine, tool grants or database writer.

The Web adapter still prepares context, model selection, exact approval,
policy, response persistence and SSE, but it no longer calls TurnService
directly when starting the Agent Loop. The stream wrapper uses explicit
nested generator closure: a new test exposed and fixed an early-close
bridge/contract context lifetime bug introduced by this additional
async boundary. No change to the existing dispatcher, durable storage
or running preview was made.

**Executable, no-HTTP proof:** [new Gateway execution tests](../../tests/test_mnerys_gateway_agent_execution.py)
build admitted authority, call the inherited runtime and consume direct
events without importing Web routes or invoking a real provider. They
also cover browser/bearer/single-user credentials, model/authority/session
mismatch, reserved runtime-field spoofing, denied tools, approved workspace
binding, and completion/early close/error/cancellation. Local validation:
**727 focused**, **185 routing**, **159 authority**, **160 peripheral**,
**1,129 contract compatibility**, **148 extra schema/tool** tests (overlap).
These are synthetic model-provider tests, **not** real model or deployment
acceptance. The exact remaining M2.1 gates are tracked in the
[bounded exit checklist](M2_1_EXIT_GATE.md); complete turn composition,
transport-neutral events and Web-versus-direct acceptance remain OPEN.

**Twenty-fifth incremental slice — one-call direct text-turn composer:**
[GatewayTurns.stream_composed_text_turn](../../src/gateway_turns.py) now
invokes a [single transport-neutral composer](../../src/gateway_turn_composer.py)
that verifies issued admission and the loaded session/owner; rejects an
account-level Agent veto **before context I/O**; calls the actual
[shared context algorithm](../../src/gateway_chat_context.py) through
injected [trusted ports](M2_CONTEXT_SERVICE_PORTS.md); builds the final
account/delegated/global policy and a sealed RequestAuthority; and streams
the inherited Agent Loop through the previously admitted execution boundary.
This is the first **one-call context → policy → authority → execution**
composition path from Python without FastAPI or the Web route.

**Deliberate limits:** This first direct path accepts only text; all tools,
including MCP, remain blocked. It does not persist either the user or model
response, so it cannot leave an orphaned chat entry while Web remains the
only complete turn-finalization writer. Since the shared context builder's
non-persisting continuation mode assumes the user turn is already in stored
history, the composer appends the preprocessed text **only to a copied
invocation prompt**, never to the cached session. Model execution remains
synthetic in tests and output remains inherited SSE frames. The production
Web route still handles its full context, tool inventory, attachments,
approvals, fallback and response saving before/after its admitted execution
call. The direct composer is **not yet a Web replacement or a public remote
API**; those are explicit pending acceptance gates.

[Composer integration tests](../../tests/test_mnerys_gateway_turn_composer.py)
cover complete direct in-process composition with browser/bearer/single-user
admissions, a model-loop stub, ownership and privilege vetoes before
port calls, immutable/non-persisted prompt handling, model policy deny-all,
and early close/error/cancellation. Existing run, owner, HTTP and context
regressions passed: **758 focused**, **185 routing**, **159 authority**,
**160 peripheral**, **1,129 contract compatibility** and
**148 extra schema/tool-edit** tests (overlapping groups). Hosted CI and
live model/preview acceptance have not run for this local commit.
The [M2.1 exit gate](M2_1_EXIT_GATE.md) marks the first composer
**PARTIAL**, with feature parity/persistence and typed events still open.

**Twenty-sixth incremental slice — owner-admitted terminal assistant saves:**
the five assistant-response save branches of
[the existing Web stream](../../routes/chat_routes.py) now call
[GatewayTurns.save_assistant_response](../../src/gateway_turns.py), which
validates the **issued admission, loaded session, context owner and claimed
session ID** before delegating to a trusted
[session-admitted persistence adapter](../../src/gateway_assistant_persistence.py).
That adapter invokes exactly the **existing**
[save_assistant_response](../../routes/chat_helpers.py) implementation,
without introducing another DB writer, transaction, schema or message
serializer. This preserves thinking/DSML cleanup, response metadata,
incognito isolation, upload/history behavior, last-access updates,
message IDs and existing save failure semantics.

Both ordinary Chat and Agent-mode successful responses, early metrics
finalization and terminal error results pass through this owner check.
Existing per-branch terminal-save flags still prevent a duplicate save on
late DONE/metrics events. **Partial replies stored on cancellation or
disconnect still use their inherited direct session writer**, as does the
synchronous Chat path. This slice does not provide atomic user+assistant
turn commits, durable idempotence or incarnation/revision fencing.
The [legacy writer inventory](SESSION_WRITER_INVENTORY.md) and
[M2.1 exit gate](M2_1_EXIT_GATE.md) retain those migration requirements.
The direct text-only composer remains intentionally **non-persisting** until
a server-owned transaction/turn finalization contract can be validated.

[Admitted save parity tests](../../tests/test_mnerys_gateway_assistant_persistence.py)
check exact passthrough of model metadata, source lists, incognito and message
ID, three credential forms, malformed arguments and forged/cross-owner
sessions **before calling the legacy saver**. Source handoff tests assert
that all five terminal save sites invoke the Gateway boundary; the original
[chat helpers regressions](../../tests/test_chat_helpers.py) remain in
the focused suite. Local tests passed **839 focused**, **185 routing**,
**159 authority**, **160 peripheral**, **1,129 compatibility** and
**148 extra tool/schema** cases (groups overlap). Hosted CI and preview
acceptance remain unverified for this local commit.

**Twenty-seventh incremental slice — admitted interrupted saves and duplicate-terminal prevention:**
both Chat and Agent cancellation/disconnect partial-response branches now call
[GatewayTurns.save_interrupted_assistant_response](../../src/gateway_turns.py),
which checks the **issued admission, loaded session, context owner, claimed
session ID, stopped metadata and incognito mode** before using the inherited
[session append/save adapter](../../routes/chat_helpers.py). The adapter
contains the same two legacy calls — append `ChatMessage("assistant")`
through the existing session object and `session_manager.save_sessions()` —
not another database writer, message serializer or transaction. Thinking
cleanup, exact selected/fallback route attribution and stored partial
metadata remain computed by the original Web stream.

An independent **per-stream** completion bug was also fixed: successful
Chat and Agent `DONE` handlers now mark terminal persistence complete
**before yielding** the saved-message event. Cancellation after a saved
terminal response cannot append a second stopped partial; duplicate DONE
cannot save another completed message. The Chat cancellation handler now
also preserves the original `CancelledError`/`GeneratorExit` and cleans
up active-stream status even if the legacy partial writer fails. Agent
already had that failure guard.

[Gateway admission and negative tests](../../tests/test_mnerys_gateway_assistant_persistence.py)
cover browser/bearer/single-user clients, forged and cross-owner sessions,
claim mismatch, incognito and malformed stopped metadata. Critically,
[real route streaming regressions](../../tests/test_foreground_model_routing.py)
exercise both modes with duplicate DONE, cancellation **after** DONE,
an interrupted-stream save failure, and existing interrupted-fallback
provenance. [Legacy detached-run and Stop tests](../../tests/test_compare_stop_disconnect_poll.py)
retain the original exact-run lifecycle. Local test suites passed
**860 focused**, **191 routing/streaming**, **159 authority/approval**,
**160 peripheral**, **1,129 contract compatibility** and
**148 external schema/tool-edit** cases (suites overlap).

This is strictly **one-attempt-per-stream** prevention, **not** durable
idempotence between workers, atomic user+assistant persistence, or
incarnation-fenced storage. Synchronous Chat and background research
writers still use their inherited paths. The direct text-only composer
still does not persist conversations. Hosted CI and live preview
acceptance remain pending for this local commit.

**Twenty-eighth incremental slice — typed, filtered direct-turn events:**
the [Gateway facade](../../src/gateway_turns.py) now offers
`open_composed_text_events(...)` for the already supported, non-persisting,
tools-disabled direct text composer. It returns
[TypedTurnDelivery with an in-process TurnOutcome](../../src/gateway_turn_events.py)
and an async iterator of filtered [TurnEvent](../../src/gateway_turn_events.py)
objects instead of requiring direct Python consumers to interpret raw SSE.
The original Agent Loop and Web streaming path **still emit and consume their
unchanged SSE frames**; a bounded transitional converter translates those
frames only in the optional direct-client path.

The converter recognizes visible text deltas, explicit final-response
replacements, sanitized numeric usage, successful DONE completion and generic
provider/Agent failures. It rejects oversized or malformed stream chunks,
filters tool/approval/reasoning events and provider diagnostic text, handles
CRLF, fragmented frames and duplicate DONE, and distinguishes incomplete
EOF from success. A closed or cancelled async generator propagates
cancellation and closes its inherited Agent Loop before security context
unbinding. The result snapshot and monotonically increasing sequence are
**process-local**, not durable turn records or replay cursors. There is no
server-issued turn ID, remote event API or cross-process idempotence.

[Transitional event contract](M2_1_TYPED_TURN_EVENTS.md) documents the exact
mapping and security limitations.
[Event protocol tests](../../tests/test_mnerys_gateway_turn_events.py)
and [direct compositor integration tests](../../tests/test_mnerys_gateway_turn_composer.py)
prove typed end-to-end execution without HTTP, admission denial before
context I/O, sanitized failure metadata, terminal ordering and cancellation.
Local suites passed **886 focused**, **191 routing/streaming**,
**159 authority**, **160 peripheral**, **1,129 contract compatibility** and
**148 extra schema/tool** tests (overlapping groups). No production Web
serialization, persistence, stop/resume or approval behavior was changed.
Hosted CI and live-model acceptance remain pending for this local commit.
The [M2.1 exit gate](M2_1_EXIT_GATE.md) remains **PARTIAL** until the
Agent Core itself emits native permission-filtered events and the complete
Web/direct lifecycle is accepted.

**Twenty-ninth incremental slice — native Agent Core completion signals (opt-in):**
the [inherited Agent Loop](../../src/agent_loop.py) now constructs a
[typed, immutable CoreTerminalEvent](../../src/agent_core_events.py)
for each of its eleven original completion yield sites **only when** a
trusted direct consumer opts in. The old path still returns the exact
`data: [DONE]` bytes by default. The [completion/evidence gate](../../src/agent_runtime/completion.py)
and [teacher takeover gate](../../src/teacher_escalation.py) now recognize
the native signal, preserve original buffering, evidence decisions, terminal
precedence, cancellation and context teardown, and emit the native signal
from the outer wrapper instead of reconstituting DONE as text. The restricted
direct native path does not launch a teacher child that might create skills;
legacy Web teacher behavior remains unchanged.

Only the [owner-admitted execution boundary](../../src/gateway_agent_execution.py)
can activate native terminal delivery for the direct composer when its
ToolPolicy **blocks all native tools and MCP** and no execution bridge is
present. Wrong credential/session/authority/model and malformed flags remain
fail-closed. [GatewayTurns.open_composed_text_events](../../src/gateway_turns.py)
opts in and its [typed event adapter](../../src/gateway_turn_events.py)
accepts CoreTerminalEvent directly, so **completion is no longer decoded
from SSE** for that path. Incremental model deltas, usage, tool events and
provider errors are still transported in inherited SSE; the application
service does not yet have fully native typed events or durable turn storage.

[Real Agent Loop emission and teacher-compatibility tests](../../tests/test_mnerys_agent_core_native_terminal.py)
verify native versus byte-identical default completion and refuse unknown
native signals. [Typed adapter](../../tests/test_mnerys_gateway_turn_events.py),
[composer](../../tests/test_mnerys_gateway_turn_composer.py) and
[execution security tests](../../tests/test_mnerys_gateway_agent_execution.py)
exercise provider errors, one terminal, contract/bridge cleanup, account
policy, no-teacher escalation and Web-safe default output. Local suites passed
**906 focused**, **191 routing**, **159 authority**, **160 peripheral**,
**1,129 compatibility**, **148 external-schema** and **196 additional
Agent Core/contract/teacher** tests (overlapping suites). One separate,
sandbox-dependent workspace subprocess E2E did not pass in this restricted
local execution environment; the relevant workspace routing case and
other core tests passed separately. Full hosted CI/real model/preview
acceptance remain pending, and the [M2.1 exit gate](M2_1_EXIT_GATE.md)
continues **PARTIAL**.

**Exit proof:** a single real Web turn can be started, observed and stopped through the new application service with identical effective tool/security behavior. No public remote API yet.

### M2.2 — Design, migrate and verify durable session writes

**Goal:** all existing session writers participate in a lifetime/revision guard.

- Use the [full writer inventory](SESSION_WRITER_INVENTORY.md) and enumerate direct writes from Web, session tools, background jobs, import/export, cleanup, admin operations and task runners. Treat unlisted direct SQL writers as blockers.
- Design additive session `revision` and `incarnation_id` fields plus background-job `origin_incarnation_id` binding. Specify backfill for existing rows and new generation allocation on session delete/recreate.
- Verify **backup and restoration** on isolated, disposable copies of both actual primary schemas; SQLite may require a transactional table rebuild, while PostgreSQL uses separately reviewed DDL.
- Adapt append, replace/truncate, rename, archive, delete, job delivery and cache reload to **owner + incarnation + expected revision** checks in atomic transactions. Preserve upload reservations, referential integrity, message counts and idempotent delivery receipts.
- Reject stale generations rather than bypass the guard; when cached state is stale, reload after commit/failed conflict, not optimistic blind append.
- Test concurrent writers, two processes, restart, deleted/recreated sessions, pending jobs, database faults, metadata parity and cancellation during persistence.

**Stop condition:** do not activate fencing on any persistent installation while a writer bypasses the protocol, or without verified backup/restore and **separate database migration approval**.

### M2.3 — Durable turn, event and approval control plane

**Goal:** client-visible streams and approval decisions survive process replacement truthfully.

- Introduce owner-checked turn records with durable status, exact turn ID, session generation, idempotency key, effective policy version and cancellation token.
- Persist ordered, versioned events with a per-turn sequence and deterministic terminal status. Define bounded retention, pagination, explicit gap handling and what is forbidden in client-visible event payloads.
- Persist sealed, owner/session/turn/resource-bound approval records; enforce digest validation, expiry, auditing and atomic single-use consumption. Reconnection or restart must never transform historical UI data into an approval grant.
- Keep effect receipts for ambiguous tool actions, preventing a retry from silently executing an external side effect again.
- Define failure and recovery behavior for interrupted model calls, cancellations, detached jobs, nested tasks and scheduler delivery under the original authority.

**Exit proof:** restart/concurrent-worker tests preserve ordering and deny unauthorized or duplicated approvals without asserting unsupported exactly-once effects.

### M2.4 — Real transport contract and cross-adapter acceptance

**Goal:** a versioned public Gateway service with at least two **real** server-admitted transport adapters, not only fake principal projections.

- Adapt the existing Web UI and API to the shared server service without breaking legacy endpoints.
- Implement the first independent scripted remote client adapter for authenticated acceptance testing. It must not import the Web server, session database, scheduler or tool executor. This is **Gateway transport proof**, not the global `mnerys` CLI distribution (M3) or the TUI (M4).
- Define and version the public request/response schema for sessions, agents/models, turns, tool proposals, approvals, events, cancel and resume from [the contract](GATEWAY_AGENT_CONTRACT.md). Implement capability discovery that never grants execution rights.
- Run the same exact-session test cases from Web and remote client: owner authorization, policy/tool denial, idempotent create, ordered event replay, cancellation by exact turn ID, approval acceptance/rejection, disconnected retry, restart, and model/provider fallback.
- No adapter may invoke model/tool execution or DB writes directly; test explicit attempted bypasses.

**Exit proof:** two actual transports exercise the same authoritative state and policy with no client-specific grants or duplicate runtime.

### M2.5 — Controlled compatibility rollout

- Validate tests and DB migrations against separate SQLite and PostgreSQL 17 fixtures, including realistic restore drills; account for auxiliary stores independently.
- Run source/privacy checks, syntax/static checks, relevant unit and Web integration tests, and security-oriented negative tests. Document optional skipped checks rather than inferring success.
- Prepare monitored migration, staged code rollback, **separate DB recovery**, health/readiness checks and rollback criteria. Do not attempt destructive migration or preview deploy without explicit approval.
- Keep the existing private native preview accessible during development; a short planned interruption is acceptable. Do not silently replace native systemd/Uvicorn with Docker Compose.
- Update the roadmap, source architecture, public API contract, tests, deployment and recovery documentation alongside implementation changes.

## M2 acceptance matrix

| Capability | Required negative or positive evidence |
| --- | --- |
| Authenticated session operations | Both real adapters share owner-scoped session history and reject foreign or unscoped access |
| Turn admission and cancellation | One active writer/session; stale turn IDs cannot stop new turns; retries use explicit idempotency |
| Tool policy and approvals | Tool execution without bound server authority fails before effects; exact sealed approvals cannot cross users, turns, resources or restarts |
| Client-visible events | Strict monotonic sequence, deterministic terminal state, explicit retention gaps, restart-safe recovery and sanitized payloads |
| Background jobs and tasks | Original owner, session incarnation and tool authority survive scheduler delivery; stale jobs fail closed |
| Database parity | SQLite and PostgreSQL 17 migrations, concurrent writers, data preservation, backup restore and rollback demonstrated separately |
| Cross-transport compatibility | A real Web adapter and an independent scripted remote adapter use the same Gateway contract; no parallel agent or permission path |
| Security and operations | Latest commit passes full CI, negative authorization tests, deployment checks and approved rollback validation |

**M2 completion cannot be declared** until this matrix has verified implementation evidence. The independent installable CLI/TUI and full UX feature parity remain later milestone obligations.

## Documentation and operational references

- [M1 design exit and remaining gaps](M1_EXIT_REVIEW.md)
- [Proposed Gateway domain objects and routes](GATEWAY_AGENT_CONTRACT.md)
- [Session writer migration requirements](SESSION_REVISION_MIGRATION.md)
- [Native preview rollout and recovery](../deployment/NATIVE_PREVIEW.md)
- [PostgreSQL 17 transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html)
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLAlchemy versioned persistence](https://docs.sqlalchemy.org/en/20/orm/versioning.html)
- [Python protocols](https://docs.python.org/3/library/typing.html#typing.Protocol)

### First implementation slice

Start with **M2.1**: audit actual request authority construction and the agent turn lifecycle, define one server-owned admission/turn service entry point, add owner/scope and unauthorized-tool regressions, and preserve current Web behavior. Treat all public routes, database schema changes, background jobs and preview deployments as separate follow-on scopes with their own validations.
