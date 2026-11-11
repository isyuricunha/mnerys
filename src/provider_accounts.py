"""M5.4 increment 2: account pools, deterministic selection, cooldowns, health.

ONE Gateway-owned account-selection contract for multiple authorized
credentials per provider. Account identity is distinct from provider and model
identity; a pool contains only accounts authorized for the SAME owner and
provider.

Selection is deterministic and explainable — never random rotation:

1. the explicitly preferred account (owner's choice);
2. owner-configured priority order (lower priority number first);
3. health state (healthy > unknown > degraded; cooling-down and unavailable
   accounts are skipped);
4. available concurrency (in-flight below the account's cap);
5. stable account-id ordering as the final tiebreak.

Rotation moves to another eligible account for the SAME provider and
compatible model/protocol (a rate-limited credential must not poison
same-URL accounts — account keys are ``url + credential fingerprint``, never
the bare host). Provider/model FALLBACK is a separate, explicitly configured
candidate chain (already owned by ``foreground_model_routing`` + the loop's
fallback events); it never runs without a trusted, user-authorized candidate
list and never widens privileges.

Rate-limit handling is provider-aware: a documented ``Retry-After`` is honored
(seconds or HTTP-date), 429-with-quota-exhaustion evidence cools longer than a
plain throttle, auth failures (401/403) never cycle credentials (they are
marked unavailable until a human fixes them), and cooldowns always expire —
no permanently stuck state, no retry storms, no unbounded queues. Cancellation
is honored immediately; a cooled account never fabricates quota.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "AccountHealthState",
    "AccountPool",
    "ProviderErrorClass",
    "classify_provider_error",
    "account_pool",
    "bind_account_pool",
    "account_health_projection",
    "account_ordered_candidates",
]

ACCOUNT_FINGERPRINT_LEN = 8
DEFAULT_TRANSIENT_COOLDOWN_SECONDS = 20.0
DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS = 0.0  # auth failures disable; they do not cycle
MAX_CONSECUTIVE_FAILURES = 3
_DEFAULT_INFLIGHT_CAP = 4

# Health states (documented, stable labels).
HEALTHY = "healthy"
DEGRADED = "degraded"
COOLING_DOWN = "cooling_down"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"
UNKNOWN = "unknown"

_STATE_RANK = {
    HEALTHY: 0,
    UNKNOWN: 1,
    DEGRADED: 2,
    COOLING_DOWN: 3,
    UNAVAILABLE: 4,
    DISABLED: 5,
}


def account_fingerprint(credentials: Any) -> str:
    """Stable, non-reversible account key from the credential reference."""
    material = str(credentials or "")
    if not material.strip():
        return ""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:ACCOUNT_FINGERPRINT_LEN]


def account_key(url: str, credentials: Any) -> str:
    """The ACCOUNT identity: same-URL accounts stay independently coolable."""
    return f"{str(url or '').strip()}|{account_fingerprint(credentials)}"


@dataclass
class AccountHealthState:
    account: str  # the account key
    state: str = UNKNOWN
    last_success: Optional[float] = None
    last_failure_category: Optional[str] = None
    last_failure_at: Optional[float] = None
    consecutive_transient_failures: int = 0
    cooldown_until: Optional[float] = None
    cooldown_reason: str = ""
    in_flight: int = 0
    last_retry_after: Optional[float] = None
    transition_reason: str = ""

    def public_projection(self) -> dict:
        """Sanitized projection: never credentials, never raw error bodies."""
        now = time.time()
        state = self.state
        if state == COOLING_DOWN and self.cooldown_until is not None and now >= self.cooldown_until:
            state = DEGRADED if self.consecutive_transient_failures else UNKNOWN
        return {
            "account_fingerprint": self.account.rsplit("|", 1)[-1] if "|" in self.account else "",
            "state": state,
            "cooldown_seconds_remaining": (
                max(0, int(self.cooldown_until - now))
                if (state == COOLING_DOWN and self.cooldown_until is not None) else 0
            ),
            "in_flight": self.in_flight,
            "consecutive_transient_failures": self.consecutive_transient_failures,
            "last_failure_category": self.last_failure_category,
            "last_retry_after": self.last_retry_after,
            "transition_reason": self.transition_reason,
        }


@dataclass(frozen=True)
class ProviderErrorClass:
    """Provider-aware failure classification (documented status semantics)."""

    category: str  # rate_limited | quota_exhausted | auth_failed | credential_invalid | permission_failed | model_unavailable | provider_overload | server_error | network | timeout_before_response | timeout_after_partial | stream_interrupted | cancelled | invalid_response | unsupported_capability | unknown
    retry_safe: bool  # a NEW request attempt is safe (no effects yet)
    cooldown_seconds: float
    account_disabling: bool = False  # needs human action (keys/subscription)
    reason: str = ""


class _AccountPool:
    """Owner-scoped account health/cooldown registry + deterministic selection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._states: Dict[str, AccountHealthState] = {}

    # ── state access ──

    def _state_for(self, key: str) -> AccountHealthState:
        state = self._states.get(key)
        if state is None:
            state = AccountHealthState(account=key)
            self._states[key] = state
        return state

    def _effective_state(self, state: AccountHealthState) -> str:
        if state.state == COOLING_DOWN and state.cooldown_until is not None:
            if time.time() >= state.cooldown_until:
                # Cooldowns always expire; a cooled account recovers to
                # degraded (one success returns it to healthy).
                state.state = DEGRADED if state.consecutive_transient_failures else UNKNOWN
                state.cooldown_until = None
                state.transition_reason = "cooldown expired"
        return state.state

    # ── selection ──

    def select(
        self, candidates: List[dict], *, requested_model: str = "",
        required_capabilities: Optional[frozenset] = None,
    ) -> tuple[Optional[dict], str]:
        """Deterministically pick the best eligible candidate.

        ``candidates``: pre-filtered, owner-authorized entries with keys
        ``url``, ``credentials``, ``model``, ``priority`` (optional),
        ``preferred`` (optional bool), ``enabled`` (optional bool),
        ``capabilities`` (optional frozenset). Returns ``(candidate, reason)``;
        the reason explains the choice or why every candidate was skipped.
        """
        if not candidates:
            return None, "no account is registered for this owner and provider"
        ranked: List[tuple] = []
        skipped: List[str] = []
        with self._lock:
            for index, candidate in enumerate(candidates):
                key = account_key(
                    str(candidate.get("url") or ""),
                    candidate.get("credentials"),
                )
                state = self._state_for(key)
                effective = self._effective_state(state)
                if candidate.get("enabled") is False:
                    skipped.append("disabled")
                    continue
                if effective in {UNAVAILABLE, DISABLED}:
                    skipped.append(f"account {effective}")
                    continue
                if effective == COOLING_DOWN:
                    remaining = int((state.cooldown_until or 0) - time.time())
                    skipped.append(f"cooling down ({max(0, remaining)}s remaining)")
                    continue
                caps = candidate.get("capabilities") or frozenset()
                if required_capabilities and not required_capabilities.issubset(caps):
                    skipped.append("missing required capabilities")
                    continue
                if candidate.get("model") and requested_model and (
                    candidate["model"] != requested_model
                ):
                    # Rotation keeps the SAME compatible model; a different
                    # model is a fallback decision, never silent rotation.
                    skipped.append("model mismatch")
                    continue
                in_flight_cap = int(candidate.get("in_flight_cap") or _DEFAULT_INFLIGHT_CAP)
                if state.in_flight >= in_flight_cap:
                    skipped.append("concurrency saturated")
                    continue
                rank = (
                    0 if candidate.get("preferred") else 1,
                    int(candidate.get("priority") or 100),
                    _STATE_RANK.get(effective, 1),
                    -state.last_success or 0 if state.last_success else 0,
                    int(candidate.get("index") or 0),  # the caller's stable order
                    index,
                )
                ranked.append((rank, candidate))
        if not ranked:
            reason = "; ".join(dict.fromkeys(skipped)) or "no eligible account"
            return None, f"no eligible account: {reason}"
        ranked.sort(key=lambda item: item[0])
        best = ranked[0][1]
        why = "preferred account" if best.get("preferred") else (
            "highest-priority eligible account"
        )
        return best, why

    # ── result recording ──

    def record_success(self, url: str, credentials: Any) -> None:
        with self._lock:
            state = self._state_for(account_key(url, credentials))
            state.state = HEALTHY
            state.last_success = time.time()
            state.consecutive_transient_failures = 0
            state.cooldown_until = None
            state.transition_reason = "request succeeded"

    def record_failure(
        self, url: str, credentials: Any, error: ProviderErrorClass,
    ) -> AccountHealthState:
        with self._lock:
            state = self._state_for(account_key(url, credentials))
            now = time.time()
            state.last_failure_category = error.category
            state.last_failure_at = now
            state.last_retry_after = error.cooldown_seconds or None
            if error.account_disabling:
                state.state = UNAVAILABLE
                state.cooldown_until = None
                state.transition_reason = error.reason or error.category
                return state
            state.consecutive_transient_failures += 1
            cooldown = error.cooldown_seconds
            if cooldown is None or cooldown <= 0:
                cooldown = (
                    DEFAULT_TRANSIENT_COOLDOWN_SECONDS
                    * min(state.consecutive_transient_failures, 3)
                )
            state.cooldown_until = now + cooldown
            state.state = COOLING_DOWN
            state.transition_reason = error.reason or error.category
            if state.consecutive_transient_failures >= MAX_CONSECUTIVE_FAILURES:
                state.state = DEGRADED
            return state

    def begin_request(self, url: str, credentials: Any) -> None:
        with self._lock:
            self._state_for(account_key(url, credentials)).in_flight += 1

    def end_request(self, url: str, credentials: Any) -> None:
        with self._lock:
            state = self._state_for(account_key(url, credentials))
            state.in_flight = max(0, state.in_flight - 1)

    def projection(self) -> List[dict]:
        with self._lock:
            return [
                self._state_for(key).public_projection()
                for key in sorted(self._states)
            ]


# Public alias: the ONE selection contract for account pools.
AccountPool = _AccountPool

_pool = _AccountPool()
_pool_binding: Optional[Any] = None
_pool_lock = threading.Lock()


def account_pool() -> _AccountPool:
    with _pool_lock:
        return _pool


def bind_account_pool(pool: _AccountPool):
    """Bind a disposable pool (isolated test fixtures)."""
    global _pool
    previous = _pool
    _pool = pool
    return lambda: globals().__setitem__("_pool", previous)


def account_health_projection() -> List[dict]:
    return account_pool().projection()


def account_ordered_candidates(candidates: List[Tuple[str, str, Any]]) -> List[Tuple[str, str, Any]]:
    """Deterministically re-order a fallback chain health-first.

    The candidate SET is unchanged — only the attempt order: cooling/down
    accounts move to the back so a rate-limited credential does not waste the
    chain's first attempts, while a fully-cooled pool still attempts every
    candidate in the caller's order (cooldowns are advisory, never a dead
    chain).
    """
    if not candidates or len(candidates) <= 1:
        return candidates
    # The chain's HEAD is the caller's deliberate primary — never reordered
    # (a fail-closed primary-route check reads position 0). Ordering applies
    # to the fallback tail only.
    primary, tail = candidates[0], list(candidates[1:])
    pool = account_pool()
    entries = [
        {
            "url": url, "credentials": headers, "model": model,
            "index": index, "enabled": True,
        }
        for index, (url, model, headers) in enumerate(tail)
    ]
    ordered: List[dict] = []
    remaining = list(entries)
    best, _reason = pool.select(remaining)
    if best is None:
        # Everything is cooled/down/disabled: cooldowns are advisory — keep
        # the caller's order so the chain still attempts every candidate.
        return candidates
    while remaining:
        ordered.append(best)
        remaining = [entry for entry in remaining if entry["index"] != best["index"]]
        if not remaining:
            break
        best, _reason = pool.select(remaining)
        if best is None:
            ordered.extend(remaining)
            break
    ordered_entries = [(entry["url"], entry["model"], entry["credentials"]) for entry in ordered]
    return [primary] + ordered_entries


# ── Provider-aware error classification ──────────────────────────────────

_RETRY_AFTER_MAX_SECONDS = 600.0
_DEFAULT_THROTTLE_SECONDS = 20.0
_QUOTA_EXHAUSTED_SECONDS = 300.0


def _parse_retry_after(value: Any) -> Optional[float]:
    """Parse a documented Retry-After header: delta-seconds or HTTP-date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        seconds = float(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = (when.timestamp() if when.tzinfo else time.mktime(when.timetuple())) - time.time()
    if seconds < 0:
        return 0.0
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)  # bounded: never trust a huge reset


def _quota_exhausted_evidence(body: Any) -> bool:
    text = str(body or "").lower()
    markers = (
        "quota", "billing", "insufficient", "exhausted", "credit",
        "payment", "plan limit", "usage limit", "subscription",
    )
    return any(marker in text for marker in markers)


def classify_provider_error(
    status_code: Any, body: Any = None, headers: Any = None,
) -> ProviderErrorClass:
    """Classify an upstream provider failure within the documented contracts.

    Distinctions that matter for safety: a 429 with quota/billing evidence is
    account exhaustion (longer cooldown, and NEVER bypassed by rotating to
    another credential of the same owner when the quota is contractual);
    authentication failures disable the account instead of cycling credentials;
    403 with permission/subscription evidence is a permission failure.
    """
    headers = headers or {}
    retry_after = _parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
    text = str(body or "")

    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return ProviderErrorClass("unknown", False, DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
                                  reason="non-numeric provider status")

    if status == 429:
        if _quota_exhausted_evidence(text):
            return ProviderErrorClass(
                "quota_exhausted", False,
                retry_after if retry_after is not None else _QUOTA_EXHAUSTED_SECONDS,
                reason="quota/billing exhaustion evidence in the provider response",
            )
        return ProviderErrorClass(
            "rate_limited", True,
            retry_after if retry_after is not None else _DEFAULT_THROTTLE_SECONDS,
            reason="throttled; Retry-After honored" if retry_after is not None else "throttled; bounded backoff",
        )
    if status in (401,):
        return ProviderErrorClass(
            "auth_failed", False, DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS,
            account_disabling=True,
            reason="authentication failed; the account needs a human to fix credentials",
        )
    if status == 403:
        if _quota_exhausted_evidence(text) or "permission" in text.lower() or "subscription" in text.lower():
            return ProviderErrorClass(
                "permission_failed", False, DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS,
                account_disabling=True,
                reason="access denied (permissions/subscription); needs human action",
            )
        return ProviderErrorClass(
            "credential_invalid", False, DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS,
            account_disabling=True,
            reason="credential rejected; needs human action",
        )
    if status == 404:
        return ProviderErrorClass(
            "model_unavailable", False, DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
            reason="endpoint/model not found",
        )
    if status == 503 or status == 529:
        return ProviderErrorClass(
            "provider_overload", True,
            retry_after if retry_after is not None else _DEFAULT_THROTTLE_SECONDS,
            reason="provider temporarily overloaded",
        )
    if status in (500, 502, 504):
        return ProviderErrorClass(
            "server_error", True,
            retry_after if retry_after is not None else _DEFAULT_THROTTLE_SECONDS,
            reason="provider server error",
        )
    return ProviderErrorClass(
        "server_error" if status >= 500 else "unknown", True,
        DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
        reason=f"provider returned HTTP {status}",
    )
