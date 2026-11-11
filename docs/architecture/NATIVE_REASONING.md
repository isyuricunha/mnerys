# M5.4 — Native Reasoning, Effort Controls and Token Accounting (Increment 4)

`src/reasoning_contract.py` is the ONE provider-independent reasoning-intent
contract. The user's preference is provider-independent (``off``/``low``/
``medium``/``high``/``max`` or an explicit token budget); each adapter
translates it ONLY into that model's documented wire fields. Unsupported
values are never fabricated: an unrecognized preference resolves to no wire
parameter with an explanatory note.

## Anthropic (verified against platform.claude.com, 2026-11)

| Generation | Thinking config | Effort | Forced tool choice |
| --- | --- | --- | --- |
| Claude 4.7+ / 5.x (Opus 5.5, Sonnet 5.5, Sonnet 5, Fable 5.1/5.2, Mythos 5.1/5.2, Haiku 5.5) | `thinking: {type: "adaptive"}` (manual budgets REJECTED — HTTP 400) | `output_config.effort` | adaptive accepts forced tool choice EXCEPT Opus 5.5 / Sonnet 5.5 / Fable 5.2 / Mythos 5.2 / Fable 5.1 / Mythos 5.1 (400) |
| Claude 4.6 (Opus/Sonnet 4.6) | adaptive (budget deprecated but accepted) | `output_config.effort` | accepted |
| Claude 4.5 and earlier thinking models | `thinking: {type: "enabled", budget_tokens: N}` (min 1024; < max_tokens) | composes with the budget ONLY on Opus 4.5 | manual mode: `auto`/`none` only |
| thinking off | `{type: "disabled"}` | — | normal |
| no preference | nothing sent (the provider default) | — | — |

Budget clamping: below-minimum budgets raise to 1024; budgets ≥ `max_tokens`
clamp to `max_tokens - 1` (documented: the budget must leave room for the
answer).

**Opaque-state round trip (three real defects fixed):**

1. **Streaming capture** — the Anthropic SSE parser previously handled only
   `text_delta`/`input_json_delta`: `thinking_delta` and `signature_delta`
   were dropped, so tool-use follow-ups lost the signatures Anthropic requires
   back complete and unmodified (HTTP 400 on the next round). The parser now
   accumulates `thinking`/`redacted_thinking` blocks verbatim (signatures
   assembled from their deltas) and emits a `thinking_blocks` event to the loop.
2. **History carry** — the loop stores `thinking_blocks` on the assistant
   message (never pruned by the reasoning-content cleanup; never
   parsed/reordered/regenerated — the accessor returns the stored list as
   received).
3. **Payload replay** — `_build_anthropic_payload` emits the stored blocks
   FIRST (before text/tool_use) exactly as received, and applies the
   per-generation thinking/effort configuration.

Privacy: signatures/redacted data ride only the internal history → payload
path; they never appear in logs, SSE frames, client history pages, tool
outputs or error responses (the history-route source is assertion-tested
free of them).

## OpenAI

Chat Completions: `reasoning_effort` where the model documents it (the
GPT-5.x-with-tools rejection already handled); `max` is NOT a documented Chat
Completions value — the intent contract annotates it rather than sending it.
Responses (subscription): reasoning items are opaque state in their
documented order — never synthesized from visible text, never stripped
merely for having no visible text.

## Ollama and OpenAI-compatible vendors

Ollama native `thinking` controls and fields unchanged (their suites);
Moonshot/Kimi fixed-temperature thinking behavior preserved; Mistral
thinking-content normalization unchanged; vendor `reasoning_content` schemas
are never cross-assumed; unsupported vendor controls are omitted rather than
guessed.

## Session preferences

`sessions.reasoning_effort` (idempotent migration) holds the owner's
preference: validated on both session surfaces (off/low/medium/high/max or a
token-budget number; an empty/whitespace value clears), threaded
server-owned into the loop (a protected field — model text can never set
it), and surviving model switches (the existing per-model effort validation
resets an unsupported effort-mode; the preference stays for capable models).
Separation of concerns: user preference → supported model settings →
effective provider request → provider-reported usage.

## Accounting (with src/model_metadata.py)

Reasoning tokens are NEVER double-counted (they are part of the reported
completion usage — tested); cache-read tokens price at the cache dimension
(never double-charged); missing usage/prices are UNKNOWN (never a misleading
zero); provider-reported cost wins verbatim; estimates are labeled estimates;
fallback attribution carries the actual served model/endpoint provenance
(`answered_by`); streamed rounds accumulate once (no duplicate final usage
chunks).

## Test evidence (increment 4)

`tests/test_native_reasoning_contract.py` — 14 deterministic tests: the
per-generation config matrix, budget clamps, the never-fabricated rule,
forced-tool-choice restrictions, the verbatim round trip (signatures +
redacted data byte-for-byte), the accessor's non-mutation, the
streaming-capture + signature assembly + usage events, the accounting
(OpenAI reasoning / Anthropic cache), the session preference
persistence/validation/model-switch, and the privacy source audit. Plus the
complete affected round: 1213 tests green.

## Remaining M5.4 work

Permitted authentication flows (Claude Code/Codex login integration),
durable account policies, and the final multi-provider acceptance — the
second INDEPENDENT provider remains gated on an authorized credential.
