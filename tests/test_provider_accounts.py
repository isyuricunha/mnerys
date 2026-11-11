"""M5.4 increment 2: account pools, deterministic selection, cooldowns, health.

One Gateway-owned selection contract for multiple authorized credentials per
provider. Account identity (url + credential fingerprint) is distinct from
provider/model identity; same-URL accounts stay independently coolable;
selection is deterministic and explainable (preferred → priority → health →
caller order); rotation never implies capability changes; fallback is the
separate, explicitly configured candidate chain.
"""
import time
from types import SimpleNamespace

import pytest

import src.provider_accounts as pa

OWNER = "alice"
from src.provider_accounts import (
    AccountPool,
    ProviderErrorClass,
    account_fingerprint,
    account_health_projection,
    account_key,
    account_ordered_candidates,
    bind_account_pool,
    classify_provider_error,
)


@pytest.fixture(autouse=True)
def _fresh_pool():
    reset = bind_account_pool(AccountPool())
    yield
    reset()


def _accounts():
    return [
        {"url": "https://p.example/v1", "credentials": {"Authorization": "Bearer key-a"},
         "model": "m", "account_id": "acct-a", "index": 0},
        {"url": "https://p.example/v1", "credentials": {"Authorization": "Bearer key-b"},
         "model": "m", "account_id": "acct-b", "index": 1},
        {"url": "https://p.example/v1", "credentials": {"Authorization": "Bearer key-c"},
         "model": "m", "account_id": "acct-c", "index": 2, "preferred": True},
    ]


def _pool():
    return pa.account_pool()


# ── Account pools and deterministic selection ─────────────────────────────

def test_preferred_account_is_selected_first():
    pick, why = _pool().select(_accounts())
    assert pick["account_id"] == "acct-c" and why == "preferred account"


def test_selection_is_deterministic_and_stable():
    picks = {_pool().select(_accounts())[0]["account_id"] for _ in range(5)}
    assert picks == {"acct-c"}  # no random rotation


def test_cooled_account_moves_back_and_healthy_accounts_serve():
    err = classify_provider_error(429, "throttled", {})
    _pool().record_failure("https://p.example/v1", {"Authorization": "Bearer key-c"}, err)
    pick, why = _pool().select(_accounts())
    assert pick["account_id"] == "acct-a", why  # stable caller order
    assert "eligible" in why


def test_rate_limited_account_never_poisons_same_url_accounts():
    err = classify_provider_error(429, "throttled", {})
    _pool().record_failure("https://p.example/v1", {"Authorization": "Bearer key-c"}, err)
    other = account_key("https://p.example/v1", {"Authorization": "Bearer key-a"})
    assert _pool()._state_for(other).state == pa.UNKNOWN  # independent cooldowns


def test_disabled_accounts_are_skipped():
    accounts = [dict(a, enabled=False) for a in _accounts()]
    pick, why = _pool().select(accounts)
    assert pick is None and "disabled" in why


def test_no_eligible_account_reports_the_reason():
    for creds in ("key-a", "key-b", "key-c"):
        _pool().record_failure(
            "https://p.example/v1", {"Authorization": f"Bearer {creds}"},
            classify_provider_error(429, "throttled", {}),
        )
    pick, why = _pool().select(_accounts())
    assert pick is None and "cooling down" in why


def test_capability_mismatch_is_skipped_deterministically():
    accounts = [dict(a, capabilities=frozenset({"native_tools"})) for a in _accounts()]
    pick, why = _pool().select(
        accounts, required_capabilities=frozenset({"native_tools", "reasoning"}),
    )
    assert pick is None and "capabilities" in why


def test_model_mismatch_is_never_silently_rotated():
    accounts = [dict(a, model="other-model") for a in _accounts()]
    pick, why = _pool().select(accounts, requested_model="gpt-x")
    assert pick is None and "model mismatch" in why


def test_concurrency_saturation_is_distinct_from_rate_limits():
    accounts = _accounts()
    url = "https://p.example/v1"
    creds = {"Authorization": "Bearer key-c"}
    for _ in range(4):
        _pool().begin_request(url, creds)
    pick, why = _pool().select(accounts)
    assert pick["account_id"] == "acct-a"  # saturated preferred → next serves
    for _ in range(4):
        _pool().end_request(url, creds)
    pick, why = _pool().select(accounts)
    assert pick["account_id"] == "acct-c"  # capacity recovered


# ── Error classification and cooldowns ────────────────────────────────────

def test_retry_after_delta_seconds_is_honored_and_bounded():
    assert classify_provider_error(429, "", {"Retry-After": "120"}).cooldown_seconds == 120
    # A huge reset never becomes an unbounded cooldown.
    huge = classify_provider_error(429, "", {"Retry-After": "9999999"})
    assert huge.cooldown_seconds == 600.0


def test_retry_after_http_date_is_parsed():
    from email.utils import formatdate

    http_date = formatdate(time.time() + 45, usegmt=True)
    result = classify_provider_error(429, "", {"Retry-After": http_date})
    assert 30 <= result.cooldown_seconds <= 45


def test_malformed_retry_after_uses_bounded_backoff():
    result = classify_provider_error(429, "", {"Retry-After": "not-a-date"})
    assert result.cooldown_seconds == 20.0
    result = classify_provider_error(429, "", {})
    assert result.cooldown_seconds == 20.0


def test_quota_exhaustion_is_distinct_from_throttling():
    throttled = classify_provider_error(429, "slow down", {})
    exhausted = classify_provider_error(429, "billing quota exhausted", {})
    assert throttled.category == "rate_limited" and throttled.retry_safe
    assert exhausted.category == "quota_exhausted"
    assert exhausted.cooldown_seconds >= 300  # quota resets are not quick


def test_auth_failures_disable_instead_of_cycling():
    for status in (401, 403):
        result = classify_provider_error(status, "invalid credentials", {})
        assert result.account_disabling
        state = _pool().record_failure(
            "https://p.example/v1", {"Authorization": "Bearer key-a"}, result,
        )
        assert state.state == pa.UNAVAILABLE
        pick, why = _pool().select(_accounts())
        assert pick["account_id"] != "acct-a"  # never cycled back
        # Reset for the next iteration of the loop.
        _pool()._states.pop(account_key("https://p.example/v1", {"Authorization": "Bearer key-a"}), None)


def test_overload_and_server_errors_use_bounded_cooldowns():
    assert classify_provider_error(503, "", {}).category == "provider_overload"
    assert classify_provider_error(529, "", {}).category == "provider_overload"
    assert classify_provider_error(502, "", {}).category == "server_error"
    assert classify_provider_error(500, "", {}).cooldown_seconds == 20.0


def test_health_recovery_after_cooldown_expiry():
    state = _pool().record_failure(
        "https://p.example/v1", {"Authorization": "Bearer key-c"},
        classify_provider_error(429, "throttled", {}),
    )
    assert state.state == pa.COOLING_DOWN
    state.cooldown_until = time.time() - 1
    accounts = _accounts()
    pick, why = _pool().select(accounts)
    assert pick["account_id"] == "acct-c"  # recovered (degraded, still eligible)
    _pool().record_success("https://p.example/v1", {"Authorization": "Bearer key-c"})
    assert _pool()._state_for(account_key("https://p.example/v1", {"Authorization": "Bearer key-c"})).state == pa.HEALTHY


def test_projection_is_sanitized():
    _pool().record_failure(
        "https://p.example/v1", {"Authorization": "Bearer super-secret-key"},
        classify_provider_error(429, "throttled", {}),
    )
    text = str(account_health_projection())
    assert "super-secret-key" not in text
    assert account_fingerprint({"Authorization": "Bearer super-secret-key"}) in text


# ── Rotation and fallback ordering ────────────────────────────────────────

def test_chain_ordering_keeps_the_primary_fixed():
    pool = AccountPool()
    err = classify_provider_error(429, "throttled", {})
    pool.record_failure("https://p.example/v1", {"Authorization": "Bearer key-a"}, err)
    cands = [
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-a"}),
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-b"}),
    ]
    ordered = account_ordered_candidates(cands)
    assert ordered[0] == cands[0]  # the primary never moves


def test_all_cooled_chain_keeps_the_callers_order():
    cands = [
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-a"}),
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-b"}),
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-c"}),
    ]
    err = classify_provider_error(429, "throttled", {})
    for creds in ("key-b", "key-c"):
        _pool().record_failure("https://p.example/v1", {"Authorization": f"Bearer {creds}"}, err)
    ordered = account_ordered_candidates(cands)
    assert [c[2]["Authorization"] for c in ordered] == [
        "Bearer key-a", "Bearer key-b", "Bearer key-c"]


def test_healthy_tail_account_rotates_forward():
    cands = [
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-a"}),
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-b"}),
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-c"}),
    ]
    _pool().record_failure(
        "https://p.example/v1", {"Authorization": "Bearer key-b"},
        classify_provider_error(429, "throttled", {}),
    )
    ordered = account_ordered_candidates(cands)
    assert [c[2]["Authorization"] for c in ordered] == [
        "Bearer key-a", "Bearer key-c", "Bearer key-b"]


def test_chain_set_is_never_changed_by_ordering():
    cands = [
        ("https://p.example/v1", "m", {"Authorization": "Bearer key-a"}),
        ("https://p2.example/v1", "m2", {"Authorization": "Bearer key-b"}),
    ]
    ordered = account_ordered_candidates(cands)
    assert len(ordered) == len(cands)
    assert {c[0] for c in ordered} == {c[0] for c in cands}


# ── Gateway visibility ────────────────────────────────────────────────────

def test_gateway_status_endpoint_is_owner_scoped_and_sanitized(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import gateway_api

    _pool().record_failure(
        "https://p.example/v1", {"Authorization": "Bearer hidden-credential"},
        classify_provider_error(429, "throttled", {}),
    )
    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(gateway_api, "_require_owner", lambda request: OWNER, raising=False)
    tc = TestClient(app)
    response = tc.get("/api/v1/provider-accounts")
    assert response.status_code == 200
    body = response.text
    assert "hidden-credential" not in body  # no credentials ever
    accounts = response.json()["accounts"]
    assert accounts and accounts[0]["state"] == "cooling_down"
