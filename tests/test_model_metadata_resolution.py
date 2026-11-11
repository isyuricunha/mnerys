"""M5.4 increment 3: model metadata resolution, the models.dev cache, and
trusted overrides.

Per-field precedence (override > provider-reported > models.dev > built-in >
unknown) with provenance; unknown is not zero; the bounded, ETag-aware,
atomic, offline-safe catalog cache; owner-scoped field-level overrides that
never grant authority.
"""
import json
import os
import tempfile
import time

import pytest
from types import SimpleNamespace

import src.model_metadata as mm
from src import constants


@pytest.fixture(autouse=True)
def _disposable_data_dir(tmp_path, monkeypatch):
    # The metadata paths read ODYSSEUS_DATA_DIR at call time — no shared-module
    # reload (reloading src.constants would poison every later test in the
    # process with a deleted disposable dir).
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    mm._cache.reset_for_tests()
    yield tmp_path
    mm._cache.reset_for_tests()


@pytest.fixture()
def seeded_catalog(_disposable_data_dir):
    catalog = {
        "anthropic": {"id": "anthropic", "models": {
            "claude-opus-4-5": {
                "id": "claude-opus-4-5", "name": "Claude Opus 4.5",
                "reasoning": True, "tool_call": True, "structured_output": True,
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "limit": {"context": 200000, "output": 64000},
                "cost": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
                "last_updated": "2025-11-24",
            },
        }},
        "openai": {"id": "openai", "models": {
            "gpt-5": {"id": "gpt-5", "name": "GPT-5", "tool_call": True,
                      "limit": {"context": 400000, "output": 128000},
                      "cost": {"input": 2.0, "output": 8.0}},
        }},
    }
    path = os.path.join(_disposable_data_dir, "models_dev_catalog.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "schema_version": 1, "source": "models_dev",
            "url": mm.MODELS_DEV_API_URL, "etag": '"v1"',
            "fetched_at": time.time(), "stale": False, "catalog": catalog,
        }, handle)
    # Reset the in-memory cache so each test reloads from its own snapshot.
    import importlib

    importlib.reload(mm)
    return catalog


# ── Resolution and precedence ─────────────────────────────────────────────

def test_models_dev_fills_context_output_and_pricing(seeded_catalog):
    meta = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5", "https://api.anthropic.com/v1", owner="alice",
    )
    assert meta.context.value == 200000 and meta.context.source == mm.MODELS_DEV_SOURCE
    assert meta.max_output.value == 64000
    assert meta.input_price.value == 5.0 and meta.output_price.value == 25.0
    assert meta.cache_read_price.value == 0.5 and meta.cache_write_price.value == 6.25
    assert meta.reasoning.value is True and meta.tool_call.value is True
    assert meta.display_name.value == "Claude Opus 4.5"


def test_provider_reported_metadata_wins_per_field(seeded_catalog):
    meta = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5", "https://capped-proxy.example/v1",
        owner="alice",
        provider_reported={"context_length": 32000},
    )
    # The capped proxy's OWN context window wins; output stays catalog-sourced.
    assert meta.context.value == 32000 and meta.context.source == "provider"
    assert meta.max_output.source == mm.MODELS_DEV_SOURCE


def test_overrides_win_per_field_and_never_clobber_siblings(seeded_catalog):
    mm.model_overrides().set_override("alice", "anthropic", "claude-opus-4-5", {
        "context": 123456, "input_price": 1.25,
    })
    meta = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5", "https://api.anthropic.com/v1", owner="alice",
    )
    assert meta.context.source == "override" and meta.context.value == 123456
    assert meta.input_price.source == "override" and meta.input_price.value == 1.25
    # Untouched siblings keep their resolved sources.
    assert meta.output_price.source == mm.MODELS_DEV_SOURCE
    assert meta.max_output.source == mm.MODELS_DEV_SOURCE


def test_overrides_are_owner_scoped(seeded_catalog):
    mm.model_overrides().set_override("alice", "anthropic", "claude-opus-4-5", {"context": 1})
    meta_bob = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5", "https://api.anthropic.com/v1", owner="bob",
    )
    assert meta_bob.context.source == mm.MODELS_DEV_SOURCE  # no cross-owner leak


def test_unknown_model_stays_unknown_never_zero(seeded_catalog):
    meta = mm.resolve_model_metadata(
        "anthropic", "not-a-real-model", "https://api.anthropic.com/v1", owner="alice",
    )
    assert meta.context.value is None and meta.context.source == "unknown"
    assert meta.input_price.value is None and meta.output_price.value is None
    assert meta.reasoning.value is None  # unknown capability, never guessed


def test_local_endpoints_do_not_match_the_community_catalog(seeded_catalog):
    meta = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5", "http://localhost:11434", owner="alice",
    )
    # Local serving never matches the COMMUNITY catalog; the built-in table
    # (previously verified local metadata) remains the documented level-4
    # source with heuristic confidence.
    assert meta.context.source == "built_in" and meta.context.confidence == "heuristic"


def test_similar_model_names_do_not_cross_map(seeded_catalog):
    meta = mm.resolve_model_metadata(
        "anthropic", "claude-opus-4-5-quantized-finetune-2024", "https://api.anthropic.com/v1",
        owner="alice",
    )
    # Exact-id matching only: a quantized/fine-tuned variant is NOT the
    # upstream models.dev record (no cross-map); the built-in table may still
    # recognize the family heuristically — but the provenance stays honest.
    assert meta.context.source != mm.MODELS_DEV_SOURCE


# ── Override validation and management ────────────────────────────────────

def test_override_validation_rejects_bad_fields_and_values(seeded_catalog):
    store = mm.model_overrides()
    with pytest.raises(ValueError):
        store.set_override("alice", "anthropic", "m", {"not_a_field": 1})
    with pytest.raises(ValueError):
        store.set_override("alice", "anthropic", "m", {"context": -5})
    with pytest.raises(ValueError):
        store.set_override("alice", "anthropic", "m", {"context": "very big"})
    with pytest.raises(ValueError):
        store.set_override("alice", "anthropic", "m", {"input_price": 999999999})
    with pytest.raises(ValueError):
        store.set_override("alice", "anthropic", "m", {})
    # No secret-shaped values are stored: only the validated typed fields.
    assert store.overrides_for("alice", "anthropic", "m") == {}


def test_override_inspect_remove_and_revision(seeded_catalog):
    store = mm.model_overrides()
    store.set_override("alice", "anthropic", "claude-opus-4-5", {"context": 5})
    store.set_override("alice", "anthropic", "claude-opus-4-5", {"context": 6})
    listed = store.list_overrides("alice")
    assert listed and listed[0]["revision"] == 2
    assert store.remove_override("alice", "anthropic", "claude-opus-4-5") is True
    assert store.remove_override("alice", "anthropic", "claude-opus-4-5") is False
    assert store.list_overrides("alice") == []


def test_capability_override_cannot_grant_execution_authority(seeded_catalog):
    # A tool_call=True override LABELS capability; it never touches admission,
    # policy or request authority. The resolver only records it with provenance.
    mm.model_overrides().set_override("alice", "openai", "gpt-5", {"tool_call": True})
    meta = mm.resolve_model_metadata("openai", "gpt-5", "https://api.openai.com/v1", owner="alice")
    assert meta.tool_call.source == "override"
    # And the policy layer is untouched by metadata:
    from src.tool_policy import ToolPolicy

    policy = ToolPolicy(disabled_tools=frozenset({"bash"}))
    assert policy.blocks("bash")  # metadata cannot change this


# ── The catalog cache: freshness, ETag, offline ───────────────────────────

def test_snapshot_reports_stale_fresh_and_offline_states(_disposable_data_dir):
    # Offline with no snapshot: unavailable, not fabricated.
    data, meta = mm.models_dev_catalog()
    assert data is None and meta["available"] is False and meta["fresh"] is False
    # A FRESH snapshot (write it directly; the fixture is not callable).
    catalog = {"anthropic": {"id": "anthropic", "models": {}}}
    with open(os.path.join(_disposable_data_dir, "models_dev_catalog.json"), "w") as handle:
        json.dump({
            "schema_version": 1, "source": "models_dev",
            "url": mm.MODELS_DEV_API_URL, "etag": '"v1"',
            "fetched_at": time.time(), "stale": False, "catalog": catalog,
        }, handle)
    import importlib

    importlib.reload(mm)
    _data, meta = mm.models_dev_catalog()
    assert meta["available"] is True and meta["fresh"] is True and meta["stale"] is False
    # An OLD snapshot reports stale explicitly (never fabricated freshness).
    old = json.load(open(os.path.join(_disposable_data_dir, "models_dev_catalog.json")))
    old["fetched_at"] = time.time() - (7 * 3600)
    with open(os.path.join(_disposable_data_dir, "models_dev_catalog.json"), "w") as handle:
        json.dump(old, handle)
    import importlib

    importlib.reload(mm)
    _data, meta = mm.models_dev_catalog()
    assert meta["fresh"] is False and meta["stale"] is True


def test_refresh_handles_304_timeout_and_malformed(tmp_path, monkeypatch, seeded_catalog):
    import httpx
    from types import SimpleNamespace as NS

    class FakeClient:
        def __init__(self, mode):
            self.mode = mode
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            self.calls += 1
            if self.mode == "304":
                return SimpleNamespace(status_code=304, headers={}, content=b"")
            if self.mode == "timeout":
                raise httpx.ReadTimeout("timed out")
            if self.mode == "big":
                return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                                       content=b"x" * (17 * 1024 * 1024))
            if self.mode == "malformed":
                return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                                       content=b"{ truncated")
            if self.mode == "wrong-type":
                return SimpleNamespace(status_code=200, headers={"content-type": "text/html"},
                                       content=b"<html/>")
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json", "etag": '"v2"'},
                                   content=json.dumps(seeded_catalog).encode())

    cache = mm._ModelsDevCache()
    # 304: the snapshot is kept and marked fresh (no poisoning).
    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient("304"))
    result = cache.refresh(force=True)
    assert result["refreshed"] is False and "304" in result["reason"]
    # Timeout/malformed/oversized/wrong-type: the snapshot is kept, the error
    # classified, and ordinary selection keeps working.
    for mode in ("timeout", "big", "malformed", "wrong-type"):
        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(mode))
        result = cache.refresh(force=True)
        assert result["refreshed"] is False, (mode, result)
        _data, meta = cache.snapshot()
        assert meta["available"] is True  # offline operation with the valid snapshot
    # A valid refresh publishes atomically with a new ETag.
    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient("ok"))
    result = cache.refresh(force=True)
    assert result["refreshed"] is True and result["etag"] == '"v2"'
    _data, meta = cache.snapshot()
    assert meta["fresh"] is True and meta["etag"] == '"v2"'


def test_refresh_dedupes_concurrent_calls(seeded_catalog, monkeypatch):
    import httpx

    slow_calls = []
    class SlowClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            slow_calls.append(1)
            time.sleep(0.3)
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json", "etag": '"v3"'},
                                   content=json.dumps(seeded_catalog).encode())

    monkeypatch.setattr(httpx, "Client", lambda **kw: SlowClient())
    import threading

    results = []
    def worker():
        try:
            results.append(("ok", mm.refresh_models_dev_catalog(force=True)))
        except Exception as exc:
            import traceback
            results.append(("ERR", traceback.format_exc()[-400:]))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The refresh deduplicates: exactly ONE network fetch for four callers.
    assert len(slow_calls) == 1, slow_calls
    assert sum(1 for kind, r in results if kind == "ok" and r.get("refreshed")) == 1, results


def test_no_ssrf_the_endpoint_is_fixed(seeded_catalog):
    from src.model_metadata import _ModelsDevCache, MODELS_DEV_API_URL
    import inspect

    source = inspect.getsource(_ModelsDevCache.refresh)
    assert "MODELS_DEV_API_URL" in source  # the fixed trusted endpoint
    assert "user_url" not in source and "refresh_url" not in source


# ── Cost estimation ───────────────────────────────────────────────────────

def test_cost_estimation_cache_aware_and_never_misleading(seeded_catalog):
    usage = {"prompt_tokens": 1000, "completion_tokens": 500,
             "prompt_tokens_details": {"cached_tokens": 400}}
    cost = mm.estimate_cost_usd_resolved(
        "claude-opus-4-5", usage, "https://api.anthropic.com/v1", owner="alice",
    )
    assert abs(cost - (600 * 5 + 500 * 25 + 400 * 0.5) / 1_000_000) < 1e-12
    # A provider-reported cost wins verbatim (no re-derivation).
    assert mm.estimate_cost_usd_resolved(
        "claude-opus-4-5", usage, "https://api.anthropic.com/v1",
        provider_reported_cost=0.0123,
    ) == 0.0123
    # Unknown pricing → unknown (never a misleading zero).
    assert mm.estimate_cost_usd_resolved(
        "not-a-real-model", usage, "https://api.anthropic.com/v1", owner="alice",
    ) is None
    # Missing usage → unknown (never fabricated quantities).
    assert mm.estimate_cost_usd_resolved(
        "claude-opus-4-5", {}, "https://api.anthropic.com/v1", owner="alice",
    ) is None
    # Cached usage with NO cache price → unknown, not zero.
    mm.model_overrides().set_override(
        "alice", "anthropic", "no-cache-price", {"input_price": 1.0, "output_price": 2.0},
    )
    assert mm.estimate_cost_usd_resolved(
        "no-cache-price",
        {"prompt_tokens": 100, "completion_tokens": 10, "cache_read_input_tokens": 50},
        "https://api.anthropic.com/v1", owner="alice",
    ) is None
    # Local endpoints are not cost-tracked.
    assert mm.estimate_cost_usd_resolved(
        "claude-opus-4-5", usage, "http://localhost:11434", owner="alice",
    ) is None


def test_reasoning_tokens_are_not_double_counted(seeded_catalog):
    # OpenAI-style reasoning tokens are ALREADY included in completion usage;
    # the estimate uses the reported completion total once.
    usage = {"prompt_tokens": 100, "completion_tokens": 200,
             "completion_tokens_details": {"reasoning_tokens": 150}}
    cost = mm.estimate_cost_usd_resolved("gpt-5", usage, "https://api.openai.com/v1", owner="alice")
    assert abs(cost - (100 * 2.0 + 200 * 8.0) / 1_000_000) < 1e-12


# ── The real models.dev schema (one bounded live fetch, already performed) ─

def test_verified_real_schema_shape(seeded_catalog):
    """The resolver's field mapping matches the VERIFIED live schema
    (fetched 2026-11-11: limit.context/output, cost.input/output/cache_read/
    cache_write, modalities, reasoning, tool_call — per-million USD)."""
    meta = mm.resolve_model_metadata("openai", "gpt-5", "https://api.openai.com/v1", owner="alice")
    assert meta.context.value == 400000 and meta.max_output.value == 128000
    assert meta.input_price.value == 2.0 and meta.output_price.value == 8.0
    assert meta.cache_read_price.value is None  # absent in this model's record: unknown, not zero


OWNER = "alice"


def test_gateway_metadata_projection(tmp_path, monkeypatch, seeded_catalog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import gateway_api

    app = FastAPI()
    app.include_router(gateway_api.router)
    monkeypatch.setattr(gateway_api, "_require_owner", lambda request: OWNER, raising=False)
    tc = TestClient(app)
    view = tc.get("/api/v1/models/metadata", params={
        "model": "claude-opus-4-5", "endpoint": "https://api.anthropic.com/v1",
    })
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["fields"]["context"]["value"] == 200000
    assert body["fields"]["context"]["source"] == mm.MODELS_DEV_SOURCE
    assert "freshness" in body["catalog_freshness"] or "fresh" in body["catalog_freshness"]
    # Overrides through the API round-trip.
    applied = tc.put("/api/v1/models/metadata/override", json={
        "provider_id": "anthropic", "model_id": "claude-opus-4-5", "fields": {"context": 999},
    })
    assert applied.status_code == 200
    assert applied.json()["fields"]["context"] == 999
    listed = tc.get("/api/v1/models/metadata/overrides")
    overrides = listed.json()["overrides"]
    assert overrides and overrides[0]["revision"] == 1
    view2 = tc.get("/api/v1/models/metadata", params={
        "model": "claude-opus-4-5", "endpoint": "https://api.anthropic.com/v1",
    })
    assert view2.json()["fields"]["context"]["source"] == "override"
    removed = tc.delete("/api/v1/models/metadata/override", params={
        "provider": "anthropic", "model": "claude-opus-4-5",
    })
    assert removed.status_code == 200
    missing = tc.delete("/api/v1/models/metadata/override", params={
        "provider": "anthropic", "model": "claude-opus-4-5",
    })
    assert missing.status_code == 404
