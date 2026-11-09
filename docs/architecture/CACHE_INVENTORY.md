# Cache inventory (verified against the code)

Evidence-based inventory of every cache the Gateway actually maintains,
for reliability and cache-accounting analysis. Verified at the runtime
reliability stabilization following M4; grep-driven and re-checked
against each module, not generated from memory.

## 1. Provider-side prompt/prefix cache (external)

- The provider (OpenAI-compatible, GLM, DeepSeek, Anthropic) caches the
  byte-identical prompt prefix across requests. Mnerys holds no state
  for it.
- Mnerys keeps the assembled prefix stable by design: system-role
  messages are consolidated and byte-identical across turns of one
  session; the per-turn timestamp block is appended as a user/context
  message near the end of the array, never inside the system prefix
  (see `src/chat_processor.py` "KV-cache friendliness" comment).
- Accounting: provider-reported cache/reasoning counts now survive into
  usage events, final turn metrics and persisted assistant metadata
  (`cached_tokens`, `reasoning_tokens`; Anthropic cache-read aggregates
  into `cached_tokens`, cache-creation is input already counted by
  Anthropic and is excluded). Never estimated; emitted only when the
  provider reports it.
- A high cached-token share (for example ~97% of a 721K-token request)
  means the prefix cache is WORKING, not a defect. What looked like
  intermittent "cache instability" was reset behavior (model or session
  switch, provider-side eviction) plus the previous invisibility of
  cache counts; the visibility gap is closed.

## 2. Settings and features file cache (`src/settings.py`)

- Key: none (two singletons). TTL: 2.0 seconds. Invalidation: time.
- Picks up operator edits within 2 seconds. Process-global; no
  per-user state.

## 3. Context-window discovery cache (`src/model_context.py`)

- `_context_cache`: key `(endpoint_url, model)` (issue #2603); value
  `(context_length, known)`. Remote endpoints only; local endpoints are
  always re-queried because they can restart with a different
  `--max-model-len`. Values equal to the default fallback are never
  cached, so a failed discovery retries next request.
- `_catalog_ctx_cache`: one catalog download per remote endpoint, keyed
  by endpoint URL.
- No TTL by design; a changed remote window needs a process restart.
  Documented here as an accepted limitation.

## 4. Probe cache (`src/agent_runtime/context_resolution.py`)

- `_probe_cache`: key `(url, model, kind)`; TTL 600s for probes with
  observations, a shorter failure TTL so a recovered endpoint is
  re-probed soon. Bounded, no payload storage.

## 5. Dead-host cooldown (`src/llm_core.py`)

- `_dead_hosts` / `_host_fails` with a dedicated lock (sync + async
  callers mutate from different threads). A host that fails twice is
  skipped for `DEAD_HOST_COOLDOWN` (20s); any success resets the
  counter immediately.

## 6. Tailscale hostname resolution (`src/endpoint_resolver.py`)

- `_tailscale_cache`: hostname → IP, resolved once via `tailscale
  status` when DNS fails; no TTL. Tailscale node IPs are stable;
  a node change needs a restart. No payload data.

## 7. Skill index corpus cache (`src/skill_index.py`)

- `_corpus_cache`: LRU of 16 embedded skill corpora (`OrderedDict` +
  lock, `move_to_end` on hit). Keyed by skill set; eviction at the
  limit. Embeddings only, no user conversation data.

## 8. Search and content disk cache (`services/search/cache.py`)

- Key: SHA-256 of the query string (or content URL); files under
  `DATA_DIR/cache/search` and `/content`. In-memory index with
  `cleanup_cache` enforcing max-age plus LRU eviction at 1000 entries;
  metrics (`hits`/`misses`/`evictions`) shared across modules.
- Isolation note: results are keyed by query, not by user; two users
  issuing an identical public web query reuse the same cached public
  result. Private content is not cached under this key scheme.

## 9. Upload handler index cache (`src/upload_handler.py`)

- Per-instance `_index_cache` plus the rate-limit cleanup tick
  (removes expired IP timestamps). No cross-user payload reuse.

## 10. Session cache reconciliation (`src/session_cache_reconciliation.py`)

- Not a data cache: it reconciles cached session state against durable
  state so a stale in-memory projection cannot outlive the durable
  record.

## Non-caches that look like caches

- `src/llm_core.py` `_LOCAL_MODEL_CURRENT` and the kimi UA header cache
  hold endpoint/model runtime identity, not prompts or responses.
- The agent runtime journal/effect adapters persist durable evidence,
  not cache entries.

## Summary of the reliability review

- No cache stores conversation payloads or authorization state; none
  grants authority. The only cross-user reuse is the public web-search
  disk cache, keyed by query hash.
- Confirmed healthy: TTL/LRU bounds exist where needed, locks guard the
  thread-mutated maps, local endpoints are never served stale context
  windows, and failed discoveries are retried instead of cached.
- Provider prefix-cache accounting is now observable end to end (usage
  events, turn metrics, persisted metadata); resets on model/session
  switches are expected behavior, not bugs.
