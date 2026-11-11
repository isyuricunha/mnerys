# M5.4 — Provider Contracts, Protocol Compatibility and Credential Isolation (Increment 1)

The verified, maintainable foundation for Mnerys' multi-provider model system.
ONE provider-independent contract inside the Agent Core/Gateway
(`src/llm_core.py` + `src/endpoint_resolver.py` + `src/gateway_turn_authority.py`);
every adapter translates only the provider wire format.

## Contract layers (one engine, no duplicates)

| Layer | Owner | Where |
| --- | --- | --- |
| Provider identity | wire-protocol family label | `_detect_provider` (hostname/subdomain + the explicit `/v1/messages` path declaration; look-alike hosts never misclassify) |
| Account/credential identity | server-owned rows | `provider_auth_sessions` (OAuth) + `model_endpoints.api_key` (generic keys); account labels distinct from provider/model IDs |
| Protocol + wire adapter | per-family serializers | Chat Completions (default), Anthropic Messages, Ollama native, Responses (chatgpt-subscription), Copilot, OpenRouter/Groq/NVIDIA/Mistral/Cerebras/Featherless/Kimi/OpenCode profiles |
| Endpoint configuration | validated rows | `model_endpoints` (base_url, api_key, kind, is_enabled, pinned models, tool modes) + `build_chat_url`/`build_headers` |
| Model identity + capabilities | per-model metadata | `model_tool_modes` (none/compact/full per model), context/output/pricing metadata (documented as `unknown` when not supplied) |
| Tool schema projection | canonical registry | the ten built-ins (wire projection via `project_tools_for_wire`); MCP names untouched |
| Streaming/response conversion | per-family parsers | SSE assembly, thinking deltas, usage/cache accounting (`extra` carries provider-reported cache/reasoning fields, shape-guarded) |
| Usage accounting | per-candidate | `candidate_request_factory` + per-run usage accumulation |
| Authorization context | Gateway admission | request authority, tool policy, principal — adapters never authorize |

## Provider compatibility matrix (audit result)

Status labels: configured → authentication validated → protocol implemented →
model listed → text verified → tool calling verified → streaming verified →
accepted.

| Provider / adapter | Protocol | Auth | Listing | Tools | Streaming | Reasoning | Coverage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenAI-compatible (default) | Chat Completions | Bearer key | `/v1/models` + pinning | native + textual fallback | SSE + usage/cache extras | provider-reported fields | protocol implemented; text/tools/streaming DET-tested; LIVE text+tools+streaming verified (FreeInference aggregator, M5.2.6/7) |
| Anthropic Messages | `/v1/messages` | `x-api-key` + version header (Bearer converted) | static catalog for anthropic.com; network list for path-declared endpoints | native tool_use/tool_result | SSE events | thinking blocks preserved | protocol implemented + path-declared variant; DET-tested; LIVE text verified |
| Ollama native | `/api/chat` | none (local) | `/api/tags` | native | NDJSON | local reasoning fields | implemented; DET-tested; local endpoints keep KV-cache affinity extras |
| ChatGPT subscription | Responses API | OAuth (device store + refresh locks; reauth/rate-limit errors) | provider metadata (reasoning levels extracted) | native | SSE | reasoning levels + opaque signatures | implemented; DET-tested; LIVE acceptance requires an authorized subscription (not assumed as paid API) |
| Copilot | Chat Completions (GitHub host) | OAuth device flow | `fetch_models` | native | SSE | provider-reported | implemented; DET-tested |
| NVIDIA NIM | OpenAI-compatible | Bearer key | listing | native | SSE | provider-reported | protocol implemented via the default adapter; DET-tested; LIVE needs credentials |
| FreeInference | Chat Completions + Anthropic Messages + Responses routes | Bearer key | `/v1/models` | native + textual | SSE | provider-reported | the M5.2 LIVE acceptance aggregator — two protocol families genuinely exercised; model-side tool compliance is provider-limited (weak free-tier models) |
| Local self-hosted (llama.cpp/LM Studio/vLLM) | OpenAI-compatible | none/explicit | `/v1/models` | native or textual | SSE + cache-prompt affinity | n/a | implemented; DET-tested (local-only extras gated to verified-local hosts) |
| Claude Code CLI login | (integration boundary) | permitted flows only | — | — | — | — | NOT integrated; any future login automation requires the official CLI flows (M5.4+ scope) |

**Honesty rules enforced:** unknown price/limits stay unknown; unknown
capabilities stay unknown unless a trusted configuration overrides them; model
capability checks are separate from user authorization (a tool-capable model
grants no permissions); a subscription is never treated as paid API quota.

## Credential isolation (audited)

- Keys live only in server-owned rows (`model_endpoints.api_key`,
  `provider_auth_sessions`); OAuth refresh uses per-auth locks.
- Model-facing API responses expose `has_key: bool` + a SHA-256 8-hex
  **fingerprint** (stable, non-reversible — `test_api_key_fingerprint_is_stable_and_non_secret`).
- No `api_key`/`Authorization` values are interpolated into logs, exceptions,
  SSE frames or tool results (audited across `llm_core`, the resolvers, the
  session store and the routes).
- Caller-supplied endpoints/headers cannot bypass registered configuration
  (raw endpoint URLs are rejected for non-admin callers); client-provided
  provider IDs resolve through owner-scoped rows only.
- Credential changes cannot redirect a sealed operation (backend-binding
  identity checks — `Approved backend binding changed`).
- Missing credentials fail clearly; local no-key endpoints stay supported when
  explicitly configured as `local`.

## Protocol selection rules (verified)

1. Ollama native (localhost detection).
2. **Explicit `/v1/messages` path declaration** → Anthropic Messages (aggregators/gateways; wire format ONLY).
3. Hostname families (anthropic.com, openrouter, groq, nvidia, moonshot/kimi,
   opencode, featherless, cerebras, mistral) — exact/subdomain matching; a
   look-alike host (`anthropic.com.example`) never misclassifies.
4. ChatGPT-subscription and Copilot OAuth bases (verified predicates).
5. Everything else → the OpenAI-compatible default.

An explicit protocol declaration selects the WIRE FORMAT only; execution,
authorization and auditing remain Gateway-owned.

## Test coverage (increment 1)

- Provider detection/URL building: `tests/test_provider_detection_builders.py`,
  `tests/test_provider_endpoints_url_building.py` (look-alike hosts, `/v1`
  collapse, path-declared Anthropic).
- Protocol serialization: the llm-core fallback/streaming suites
  (`test_llm_core_fallback.py`), tool-mode negotiation (`test_model_tool_modes.py`).
- Credential isolation: the fingerprint test + the endpoint-route suites
  (create/update round-trips never expose the key).
- Live two-protocol acceptance: M5.2's harness (text+tools+streaming through
  Chat Completions and Anthropic Messages).
- The scheduler/endpoint authority suite (provider selection cannot widen
  authority; enabled/visible registration required).


## Increment 3 — model metadata, cache, pricing and trusted overrides (implemented)

`src/model_metadata.py` resolves model metadata with PER-FIELD precedence and
provenance: owner-scoped trusted overrides → authoritative provider-reported
values → the cached models.dev catalog (the FIXED trusted endpoint;
`https://models.dev/api.json`; the schema verified live 2026-11-11 —
`limit.context/output`, `cost.input/output/cache_read/cache_write` priced in
USD per million tokens, `modalities`, `reasoning`, `tool_call`) → the built-in
tables (heuristic) → unknown. Unknown is explicit and never zero. The cache is
bounded (TTL, ETag/If-None-Match conditional requests with 304 handling, atomic
publication, size/type/schema validation, refresh deduplication, offline
operation with explicit stale/fresh status, no billable inference, no SSRF).
Overrides are owner-scoped, validated, inspectable, removable and
revision-counted; they label capabilities but never grant tool execution
authority or alter policy. Exact-id matching only (no variant cross-mapping);
local endpoints never match the community catalog. Cost estimation uses
provider-reported costs when present, prices cached tokens at the cache
dimension (never double-charged), never double-counts reasoning tokens, and
returns UNKNOWN (never a misleading zero) for missing usage/prices.
`GET /api/v1/models/metadata` (+ override list/set/remove + controlled
refresh) exposes the sanitized owner-scoped projection with freshness
provenance. Evidence: `tests/test_model_metadata_resolution.py` (18
deterministic tests) + the model (75) and provider (222) suites.

## Remaining M5.4 work (subsequent increments)

Multi-account pools/rotation, provider health/cooldown scheduling and safe
fallback (distinct from rotation; never blind retry of side-effecting
operations), models.dev metadata cache with TTL/ETag + provenance + per-model
overrides, pricing/context resolution UI, reasoning-effort configuration UI,
Claude Code/Codex login-flow integration under permitted terms, and the hosted
two-independent-provider acceptance (an authorized second provider).
