# M5.4 — Account Pools, Health, Cooldowns and Safe Fallback (Increment 2)

`src/provider_accounts.py` is the ONE Gateway-owned account-selection contract
for multiple authorized credentials per provider. Account identity is distinct
from provider and model identity; a pool contains only accounts authorized for
the same owner and provider; selection is deterministic and explainable —
never random rotation.

## Account identity and storage

- **OAuth accounts**: `provider_auth_sessions` (owner-scoped, labeled,
  encrypted tokens, per-auth refresh locks) — unchanged.
- **API-key accounts**: `model_endpoints` rows (owner-scoped keys) — multiple
  rows for the same provider URL with different keys are legitimate accounts
  (the fallback dedupe already treats credentials as part of route identity).
- **Account key** = endpoint URL + credential fingerprint (SHA-256, 8 hex,
  non-reversible). One account's cooldown never poisons same-URL accounts.
- Credentials are never returned to clients (the API exposes `has_key` +
  fingerprint only); cross-owner credential access is denied by the existing
  owner-scoped row filters.

## Deterministic selection (one contract)

`AccountPool.select(candidates, requested_model, required_capabilities)`:

1. the explicitly preferred account;
2. owner-configured priority (lower first);
3. health (healthy > unknown > degraded; cooling-down/unavailable/disabled
   skipped with the reason recorded);
4. available concurrency (in-flight below the account cap);
5. the caller's candidate order as the final tiebreak (stable, reproducible,
   audit-friendly).

Model mismatch is never silently rotated (a different model is a FALLBACK
decision); capability requirements must be satisfied (missing capabilities are
skipped with the reason); a fully-saturated pool moves to the next account;
when NO account is eligible the reason names every skip (cooldown remainders,
disabled, capability gaps).

## Rotation versus fallback

**Rotation** = another eligible account for the SAME provider and compatible
model/protocol (a rate-limited credential must not waste the chain). The
fallback chains are deterministically re-ordered health-first with the
primary (the caller's deliberate head) NEVER moved — the fail-closed
primary-route checks read position 0. An all-cooled pool keeps the caller's
order (cooldowns are advisory; the chain never goes dead).

**Fallback** = the explicitly configured provider/model candidate chain
(`foreground_model_routing` + the loop's `fallback`/`answered_by` events with
per-candidate provenance). Unchanged in this increment: user-authorized
candidates only, capability-compatible only, no privilege expansion, and no
silent reasoning/tool-capability downgrades (the candidate factories
negotiate per-candidate capabilities).

## Error classification (provider-aware)

`classify_provider_error(status, body, headers)`:

| Category | Trigger | Cooldown | Effect |
| --- | --- | --- | --- |
| `rate_limited` | 429 without quota evidence | documented Retry-After (bounded 600s) or 20s backoff | retry-safe; the account cools |
| `quota_exhausted` | 429 with quota/billing evidence | Retry-After or 300s | longer cooldown; contractual limits are never bypassed by rotation |
| `auth_failed` / `credential_invalid` / `permission_failed` | 401 / 403 | — | the account becomes **unavailable** (needs a human); credentials never cycle |
| `model_unavailable` | 404 | 20s | the endpoint/model is skipped |
| `provider_overload` | 503 / 529 | Retry-After or 20s | retry-safe |
| `server_error` | 500/502/504 | Retry-After or 20s | retry-safe |
| unknown | non-numeric/other | 20s | conservative |

Retry-After parses delta-seconds AND HTTP-dates; malformed/missing metadata
uses bounded backoff; huge resets are capped (600s) — never trusted as
unbounded.

## Health states

`healthy` → `degraded` (≥3 consecutive transient failures) → `cooling_down`
(bounded, always expires) → `unavailable` (auth/permission failures — needs a
human) → `disabled` (owner action). Every transition records the reason; the
projection is sanitized (fingerprint-only identity, cooldown remainder,
in-flight count, failure category, last Retry-After — never credentials or
raw provider bodies). Passive signals only (real requests); no billable
health probes.

## Side-effect safety (the critical boundary)

Provider rotation/fallback is NOT a turn restart — the existing loop
contract is preserved and verified:

- Failover happens only on **pre-content** failures: once a candidate has
  emitted real output, the chain NEVER switches (no duplicated streamed
  tokens) — a later error passes through unchanged.
- Tool execution happens BETWEEN rounds under the Gateway's admission/approval
  machinery; a provider failure mid-round never replays completed tools —
  durable tool results and execution receipts are authoritative.
- Fallback events are distinguishable in the stream (`answered_by`/model
  provenance), so recovery is never silently merged into the primary answer.
- Retries are bounded (`max_retries`); cancellation is honored immediately
  (no retry after cancellation); no recursive/unbounded retry chains.
- If effect state is ever uncertain, the run fails closed (the
  artifact-evidence completion gate refuses to claim success without real
  mutation evidence).

## Gateway visibility

`GET /api/v1/provider-accounts` — the sanitized health/cooldown projection
(owner-scoped). The existing model-endpoints UI remains the account
management surface (enable/disable/keys); no duplicate dashboard.

## Test evidence (increment 2)

`tests/test_provider_accounts.py` — 22 deterministic tests: preferred/priority
selection, determinism, cooled-account ordering with same-URL independence,
disabled/permission skips with reasons, capability/model mismatch refusal,
concurrency saturation vs rate limits, Retry-After (delta/HTTP-date/malformed/
bounded), quota vs throttle, auth-failure disabling without cycling, health
recovery, sanitized projections, primary-fixed chain ordering, all-cooled
caller order, set preservation, and the owner-scoped status endpoint. Plus
the regression rounds: the llm-core fallback/concurrency/ollama suites (103),
the provider detection/URL/tool-mode suites (216).

## Known limitations (documented)

- The account fingerprint is per-process-memory state (health/cooldowns reset
  on restart) — cooldowns are advisory and short by design; durable rotation
  policy is a later increment.
- Quota dimensions are per-account, not per-provider-dimension (headers/
  tokens/requests collapse into one cooldown) — provider-specific quota
  parsing lands with the metadata cache increment.
- No active health probes (passive signals only — by design).
- Fallback candidate configuration remains the existing trusted route
  configuration; a dedicated fallback-priority UI is a later increment.
