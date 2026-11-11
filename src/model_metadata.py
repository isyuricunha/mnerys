"""M5.4 increment 3: ONE model-metadata resolution contract.

Combines, per FIELD (never by replacing whole records):

1. Explicit authorized per-model overrides (owner-scoped, trusted).
2. Verified authoritative provider metadata (endpoint-reported).
3. The cached models.dev catalog (https://models.dev/api.json — the fixed,
   trusted endpoint; community metadata is a FALLBACK, never a replacement
   for authoritative provider data).
4. Previously verified local metadata (the built-in tables).
5. Unknown — an explicit nullable state; **unknown is not zero**.

Provenance (source, confidence, timestamp) accompanies every resolved field.
Metadata never grants tool execution authority and never overrides Gateway
policy: capability overrides label what a model can do; admission, policy and
request authority decide what MAY run.

models.dev schema (verified 2026-11-11, ~6 MB, 235 providers):
``{provider: {id, name, doc, models: {model_id: {id, name, description,
family, attachment, reasoning, reasoning_options, tool_call,
structured_output, temperature, modalities: {input, output},
limit: {context, output}, cost: {input, output, cache_read, cache_write},
release_date, last_updated}}}}``. Prices are USD **per million tokens** —
the same unit convention as the built-in pricing table; values are never
converted blindly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "MODELS_DEV_API_URL",
    "MODELS_DEV_SOURCE",
    "ModelMetadata",
    "models_dev_catalog",
    "refresh_models_dev_catalog",
    "resolve_model_metadata",
    "model_overrides",
    "estimate_cost_usd_resolved",
]

MODELS_DEV_API_URL = "https://models.dev/api.json"
MODELS_DEV_SOURCE = "models_dev"
_MODELS_DEV_PROVIDER_ALIASES = {
    # Provider-namespace alignment: Mnerys provider labels → models.dev ids.
    # Verified against the catalog's own provider ids (no guessing).
    "anthropic": {"anthropic"},
    "openai": {"openai"},
    "google": {"google", "google-vertex"},
    "mistral": {"mistral"},
    "deepseek": {"deepseek"},
    "xai": {"xai"},
    "groq": {"groq"},
    "openrouter": {"openrouter"},
    "nvidia": {"nvidia"},
    "moonshot": {"moonshot"},
    "ollama": {"ollama"},
    "lmstudio": {"lmstudio"},
}
_DEFAULT_TTL_SECONDS = 6 * 3600
_MAX_CATALOG_BYTES = 16 * 1024 * 1024
_FETCH_TIMEOUT = 20.0

_FIELD_SOURCES = ("override", "provider", "models_dev", "built_in", "unknown")


@dataclass
class ResolvedField:
    value: Any = None
    source: str = "unknown"
    confidence: str = "unknown"
    verified_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "verified_at": self.verified_at,
        }


@dataclass
class ModelMetadata:
    provider_id: str
    model_id: str
    context: ResolvedField = dc_field(default_factory=ResolvedField)
    max_output: ResolvedField = dc_field(default_factory=ResolvedField)
    input_price: ResolvedField = dc_field(default_factory=ResolvedField)   # USD / 1M tokens
    output_price: ResolvedField = dc_field(default_factory=ResolvedField)  # USD / 1M tokens
    cache_read_price: ResolvedField = dc_field(default_factory=ResolvedField)
    cache_write_price: ResolvedField = dc_field(default_factory=ResolvedField)
    reasoning: ResolvedField = dc_field(default_factory=ResolvedField)
    reasoning_options: ResolvedField = dc_field(default_factory=ResolvedField)
    tool_call: ResolvedField = dc_field(default_factory=ResolvedField)
    streaming: ResolvedField = dc_field(default_factory=ResolvedField)
    modalities: ResolvedField = dc_field(default_factory=ResolvedField)
    display_name: ResolvedField = dc_field(default_factory=ResolvedField)

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "fields": {
                name: getattr(self, name).to_dict()
                for name in (
                    "context", "max_output", "input_price", "output_price",
                    "cache_read_price", "cache_write_price", "reasoning",
                    "reasoning_options", "tool_call", "streaming", "modalities",
                    "display_name",
                )
            },
        }


def _unknown(value: Any = None) -> ResolvedField:
    return ResolvedField(value=value, source="unknown", confidence="unknown")


# ── models.dev catalog cache ─────────────────────────────────────────────

def _data_dir() -> str:
    """The data dir, honoring ODYSSEUS_DATA_DIR at call time (no shared-module
    reload churn: importing ``src.constants.DATA_DIR`` would freeze the value
    at import and a test's env change would not apply)."""
    from src.constants import DATA_DIR as _default

    return os.environ.get("ODYSSEUS_DATA_DIR") or _default


def _catalog_path() -> str:
    return os.path.join(_data_dir(), "models_dev_catalog.json")


def _validated_catalog(raw: Any) -> Optional[dict]:
    """Schema/type validation: reject malformed, truncated or foreign shapes."""
    if not isinstance(raw, dict) or not raw:
        return None
    providers = 0
    for provider_id, provider in raw.items():
        if not isinstance(provider_id, str) or not isinstance(provider, dict):
            return None
        models = provider.get("models")
        if models is not None and not isinstance(models, dict):
            return None
        if isinstance(models, dict):
            for model_id, model in models.items():
                if not isinstance(model_id, str) or not isinstance(model, dict):
                    return None
        providers += 1
    return raw if providers else None


class _ModelsDevCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._refreshing = False
        self._data: Optional[dict] = None
        self._etag: Optional[str] = None
        self._fetched_at: Optional[float] = None
        self._stale = True
        self._last_error: Optional[str] = None

    def _load_snapshot_locked(self) -> None:
        if self._data is not None:
            return
        try:
            with open(_catalog_path(), encoding="utf-8") as handle:
                snapshot = json.load(handle)
            if not isinstance(snapshot, dict):
                return
            self._data = snapshot.get("catalog") if isinstance(snapshot.get("catalog"), dict) else None
            self._etag = snapshot.get("etag")
            self._fetched_at = snapshot.get("fetched_at")
            self._stale = snapshot.get("stale", True)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("models.dev catalog snapshot unreadable; ignoring it")

    def snapshot(self) -> Tuple[Optional[dict], dict]:
        """The current catalog + freshness metadata (offline-safe)."""
        with self._lock:
            self._load_snapshot_locked()
            fresh = (
                self._fetched_at is not None
                and not self._stale
                and (time.time() - self._fetched_at) < _DEFAULT_TTL_SECONDS
            )
            return self._data, {
                "source": MODELS_DEV_SOURCE,
                "url": MODELS_DEV_API_URL,
                "fetched_at": self._fetched_at,
                "fresh": fresh,
                "stale": bool(self._data is not None and not fresh),
                "etag": self._etag,
                "last_error": self._last_error,
                "available": self._data is not None,
            }

    def lookup(self, provider_ids: set[str], model_id: str) -> Optional[dict]:
        """Exact-id lookup scoped to the verified provider ids."""
        data, _meta = self.snapshot()
        if not data:
            return None
        for provider_id in provider_ids:
            provider = data.get(provider_id)
            if not isinstance(provider, dict):
                continue
            models = provider.get("models") or {}
            model = models.get(model_id)
            if isinstance(model, dict):
                return model
        return None

    def refresh(self, *, force: bool = False, transport=None) -> dict:
        """Bounded, deduplicated refresh with ETag conditional requests.

        Never blocks ordinary model selection: a failure keeps the existing
        snapshot (stale) and records the error classification.
        """
        with self._lock:
            if self._refreshing:
                return {"refreshed": False, "reason": "refresh already in progress"}
            self._load_snapshot_locked()
            if (
                not force and self._data is not None and not self._stale
                and self._fetched_at is not None
                and (time.time() - self._fetched_at) < _DEFAULT_TTL_SECONDS
            ):
                return {"refreshed": False, "reason": "cache fresh"}
            self._refreshing = True
        try:
            import httpx

            headers = {}
            if self._etag:
                headers["If-None-Match"] = self._etag
            try:
                with httpx.Client(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
                    response = client.get(MODELS_DEV_API_URL, headers=headers)
                if response.status_code == 304 and self._data is not None:
                    with self._lock:
                        self._fetched_at = time.time()
                        self._stale = False
                        self._save_snapshot_locked()
                    return {"refreshed": False, "reason": "not modified (304)", "etag": self._etag}
                if response.status_code != 200:
                    self._record_error(f"HTTP {response.status_code}")
                    return {"refreshed": False, "reason": f"HTTP {response.status_code}"}
                if len(response.content) > _MAX_CATALOG_BYTES:
                    self._record_error("catalog response exceeds the size limit")
                    return {"refreshed": False, "reason": "response too large"}
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    self._record_error(f"unexpected content type {content_type!r}")
                    return {"refreshed": False, "reason": "unexpected content type"}
                catalog = _validated_catalog(json.loads(response.content))
                if catalog is None:
                    self._record_error("malformed catalog structure")
                    return {"refreshed": False, "reason": "malformed catalog"}
                with self._lock:
                    self._data = catalog
                    self._etag = response.headers.get("etag")
                    self._fetched_at = time.time()
                    self._stale = False
                    self._last_error = None
                    self._save_snapshot_locked()
                return {"refreshed": True, "providers": len(catalog), "etag": self._etag}
            except (httpx.HTTPError, ValueError, OSError) as exc:
                self._record_error(f"{type(exc).__name__}: {exc}"[:200])
                return {"refreshed": False, "reason": "fetch failed; snapshot kept"}
        finally:
            with self._lock:
                self._refreshing = False

    def reset_for_tests(self) -> None:
        with self._lock:
            self._data = None
            self._etag = None
            self._fetched_at = None
            self._stale = True
            self._last_error = None
            self._refreshing = False

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._stale = True

    def _save_snapshot_locked(self) -> None:
        path = _catalog_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "schema_version": 1,
            "source": MODELS_DEV_SOURCE,
            "url": MODELS_DEV_API_URL,
            "etag": self._etag,
            "fetched_at": self._fetched_at,
            "stale": self._stale,
            "catalog": self._data,
        }
        temp = f"{path}.tmp-{os.getpid()}"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temp, path)


_cache = _ModelsDevCache()


def models_dev_catalog() -> Tuple[Optional[dict], dict]:
    return _cache.snapshot()


def refresh_models_dev_catalog(*, force: bool = False, transport=None) -> dict:
    """Controlled manual/periodic refresh (no billable inference involved)."""
    return _cache.refresh(force=force, transport=transport)


# ── Trusted owner-scoped overrides ───────────────────────────────────────

_OVERRIDE_FIELDS = {
    "context": int, "max_output": int, "input_price": (int, float),
    "output_price": (int, float), "cache_read_price": (int, float),
    "cache_write_price": (int, float), "reasoning": bool, "tool_call": bool,
    "streaming": bool,
}
_MAX_OVERRIDES_PER_OWNER = 512
_OVERRIDE_ATTRS = {
    "context": "context", "max_output": "max_output", "input_price": "input_price",
    "output_price": "output_price", "cache_read_price": "cache_read_price",
    "cache_write_price": "cache_write_price", "reasoning": "reasoning",
    "tool_call": "tool_call", "streaming": "streaming",
}


class OverrideStore:
    """Owner-scoped, per-model field overrides (trusted configuration)."""

    def __init__(self):
        self._lock = threading.Lock()

    def _path(self) -> str:
        return os.path.join(_data_dir(), "model_metadata_overrides.json")

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) and data.get("version") == 1 else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("model metadata overrides unreadable; ignoring them")
            return {}

    def _save(self, data: dict) -> None:
        payload = {"version": 1, **data}
        path = self._path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp = f"{path}.tmp-{os.getpid()}"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(temp, path)

    def set_override(
        self, owner: str, provider_id: str, model_id: str,
        fields: Dict[str, Any],
    ) -> dict:
        """Apply field-level overrides with strict type/range validation."""
        clean_owner = str(owner or "").strip().casefold()
        clean_provider = str(provider_id or "").strip().lower()
        clean_model = str(model_id or "").strip()
        if not clean_owner or not clean_provider or not clean_model:
            raise ValueError("owner, provider and model are required")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("an override needs at least one field")
        validated: Dict[str, Any] = {}
        for name, value in fields.items():
            expected = _OVERRIDE_FIELDS.get(str(name))
            if expected is None:
                raise ValueError(f"unsupported override field: {name}")
            try:
                if expected is bool:
                    if not isinstance(value, bool):
                        raise ValueError
                elif isinstance(value, bool) or not isinstance(value, expected):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(f"override field {name} has an invalid value type")
            if str(name) in ("context", "max_output") and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"override field {name} must be a positive integer")
            if "price" in str(name) and (value < 0 or value > 100000):
                raise ValueError(f"override field {name} is out of range")
            validated[str(name)] = value
        with self._lock:
            data = self._load()
            owners = data.setdefault("owners", {})
            owner_records = owners.setdefault(clean_owner, {})
            if len(owner_records) >= _MAX_OVERRIDES_PER_OWNER and (
                f"{clean_provider}:{clean_model}" not in owner_records
            ):
                raise ValueError("too many saved overrides for this owner")
            record = owner_records.get(f"{clean_provider}:{clean_model}") or {}
            entries = record.get("fields") or {}
            entries.update(validated)
            owner_records[f"{clean_provider}:{clean_model}"] = {
                "fields": entries,
                "updated_at": time.time(),
                "revision": int(record.get("revision") or 0) + 1,
            }
            self._save(data)
            return {"provider_id": clean_provider, "model_id": clean_model, "fields": entries}

    def remove_override(self, owner: str, provider_id: str, model_id: str) -> bool:
        clean_owner = str(owner or "").strip().casefold()
        clean_provider = str(provider_id or "").strip().lower()
        clean_model = str(model_id or "").strip()
        with self._lock:
            data = self._load()
            owners = data.get("owners") or {}
            owner_records = owners.get(clean_owner) or {}
            key = f"{clean_provider}:{clean_model}"
            if key not in owner_records:
                return False
            owner_records.pop(key, None)
            if not owner_records:
                owners.pop(clean_owner, None)
            self._save(data)
            return True

    def overrides_for(self, owner: str, provider_id: str, model_id: str) -> Dict[str, Any]:
        clean_owner = str(owner or "").strip().casefold()
        key = f"{str(provider_id or '').strip().lower()}:{str(model_id or '').strip()}"
        with self._lock:
            data = self._load()
            record = ((data.get("owners") or {}).get(clean_owner) or {}).get(key) or {}
            return dict(record.get("fields") or {})

    def list_overrides(self, owner: str) -> List[dict]:
        clean_owner = str(owner or "").strip().casefold()
        with self._lock:
            data = self._load()
            records = (data.get("owners") or {}).get(clean_owner) or {}
            items = []
            for key, record in sorted(records.items()):
                provider_id, _, model_id = key.partition(":")
                items.append({
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "fields": record.get("fields"),
                    "updated_at": record.get("updated_at"),
                    "revision": record.get("revision"),
                })
            return items


_overrides = OverrideStore()


def model_overrides() -> OverrideStore:
    return _overrides


# ── Resolution ───────────────────────────────────────────────────────────

def _models_dev_provider_ids(provider_hint: Optional[str], endpoint_url: Optional[str]) -> set[str]:
    """The models.dev provider ids to consider for this endpoint (no guessing)."""
    from src.model_context import is_local_endpoint

    if is_local_endpoint(endpoint_url):
        # Local servers are not community-catalog providers unless explicitly
        # configured — unknown stays unknown.
        return set()
    from src.llm_core import _detect_provider

    detected = _detect_provider(endpoint_url or "")
    aliases = set(_MODELS_DEV_PROVIDER_ALIASES.get(detected, set()))
    if provider_hint:
        aliases |= _MODELS_DEV_PROVIDER_ALIASES.get(str(provider_hint).lower(), set())
    return aliases


def resolve_model_metadata(
    provider_hint: Optional[str],
    model_id: str,
    endpoint_url: Optional[str] = None,
    *,
    owner: Optional[str] = None,
    provider_reported: Optional[dict] = None,
) -> ModelMetadata:
    """Resolve one model's metadata with per-field precedence + provenance."""
    clean_model = str(model_id or "").strip()
    metadata = ModelMetadata(
        provider_id=str(provider_hint or "").strip().lower() or "unknown",
        model_id=clean_model,
    )
    catalog_model = _cache.lookup(
        _models_dev_provider_ids(provider_hint, endpoint_url), clean_model,
    ) if clean_model else None

    def apply(target: ResolvedField, value: Any, source: str, confidence: str) -> ResolvedField:
        if value is None or target.source != "unknown":
            return target
        return ResolvedField(
            value=value, source=source, confidence=confidence, verified_at=time.time(),
        )

    # 1. Trusted overrides (per field).
    override_fields = (
        model_overrides().overrides_for(owner, metadata.provider_id, clean_model)
        if owner and clean_model else {}
    )
    for name, attr in _OVERRIDE_ATTRS.items():
        if name in override_fields:
            setattr(metadata, attr, apply(
                getattr(metadata, attr), override_fields[name], "override", "explicit",
            ))

    # 2. Authoritative provider-reported metadata (per field).
    if isinstance(provider_reported, dict):
        context = _positive_int(provider_reported.get("context_length") or provider_reported.get("context_window") or provider_reported.get("max_model_len"))
        metadata.context = apply(metadata.context, context, "provider", "provider_reported")
        max_out = _positive_int(provider_reported.get("max_output_tokens") or provider_reported.get("max_completion_tokens"))
        metadata.max_output = apply(metadata.max_output, max_out, "provider", "provider_reported")

    # 3. models.dev (fallback, per field).
    if isinstance(catalog_model, dict):
        limit = catalog_model.get("limit") or {}
        metadata.context = apply(metadata.context, _positive_int(limit.get("context")), MODELS_DEV_SOURCE, "registry")
        metadata.max_output = apply(metadata.max_output, _positive_int(limit.get("output")), MODELS_DEV_SOURCE, "registry")
        cost = catalog_model.get("cost") or {}
        metadata.input_price = apply(metadata.input_price, _non_negative_number(cost.get("input")), MODELS_DEV_SOURCE, "registry")
        metadata.output_price = apply(metadata.output_price, _non_negative_number(cost.get("output")), MODELS_DEV_SOURCE, "registry")
        metadata.cache_read_price = apply(metadata.cache_read_price, _non_negative_number(cost.get("cache_read")), MODELS_DEV_SOURCE, "registry")
        metadata.cache_write_price = apply(metadata.cache_write_price, _non_negative_number(cost.get("cache_write")), MODELS_DEV_SOURCE, "registry")
        metadata.reasoning = apply(metadata.reasoning, catalog_model.get("reasoning"), MODELS_DEV_SOURCE, "registry")
        metadata.reasoning_options = apply(
            metadata.reasoning_options, catalog_model.get("reasoning_options"), MODELS_DEV_SOURCE, "registry",
        )
        metadata.tool_call = apply(metadata.tool_call, catalog_model.get("tool_call"), MODELS_DEV_SOURCE, "registry")
        metadata.modalities = apply(metadata.modalities, catalog_model.get("modalities"), MODELS_DEV_SOURCE, "registry")
        metadata.display_name = apply(metadata.display_name, catalog_model.get("name"), MODELS_DEV_SOURCE, "registry")

    # 4. Previously verified local metadata.
    if metadata.context.source == "unknown":
        try:
            from src.model_context import get_context_length_known

            ctx, known = get_context_length_known(endpoint_url, clean_model)
            if known:
                metadata.context = apply(metadata.context, ctx, "built_in", "heuristic")
        except Exception:
            pass
    if metadata.input_price.source == "unknown":
        try:
            from src.model_pricing import MODEL_PRICING, match_model_key

            key = match_model_key(clean_model, MODEL_PRICING.keys())
            if key:
                price_in, price_out = MODEL_PRICING[key]
                metadata.input_price = apply(metadata.input_price, price_in, "built_in", "heuristic")
                metadata.output_price = apply(metadata.output_price, price_out, "built_in", "heuristic")
        except Exception:
            pass
    return metadata


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _non_negative_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


# ── Cost estimation integration ──────────────────────────────────────────

def estimate_cost_usd_resolved(
    model: str | None,
    usage: dict | None,
    endpoint_url: str | None = None,
    *,
    owner: Optional[str] = None,
    provider_hint: Optional[str] = None,
    provider_reported_cost: Optional[float] = None,
) -> Optional[float]:
    """Estimated USD cost from resolved metadata + ACTUAL structured usage.

    Precedence: a provider-REPORTED cost (e.g. OpenRouter ``usage.cost``)
    wins; otherwise resolved per-dimension pricing × the provider-reported
    token usage. Cached input tokens are priced at the cache-read dimension
    (never double-charged); reasoning tokens are never double-counted (they
    are part of the reported output/completion usage). Unknown prices or
    missing usage produce UNKNOWN (None) — never a misleading zero.
    """
    if provider_reported_cost is not None:
        try:
            cost = float(provider_reported_cost)
            return cost if cost >= 0 else None
        except (TypeError, ValueError):
            pass
    if not model or not isinstance(usage, dict):
        return None
    from src.model_pricing import is_cost_tracked_endpoint

    if not is_cost_tracked_endpoint(endpoint_url):
        return None
    metadata = resolve_model_metadata(
        provider_hint, model, endpoint_url, owner=owner,
    )
    input_price = metadata.input_price.value
    output_price = metadata.output_price.value
    cache_read_price = metadata.cache_read_price.value
    if input_price is None or output_price is None:
        return None  # unknown pricing stays unknown

    def _count(*names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return 0

    input_tokens = _count("prompt_tokens", "input_tokens", "input")
    output_tokens = _count("completion_tokens", "output_tokens", "output")
    cached_tokens = _count(
        "cache_read_input_tokens", "prompt_tokens_details",
        "cached_tokens", "cache_read_tokens",
    )
    if isinstance(usage.get("prompt_tokens_details"), dict):
        cached_tokens = int(usage["prompt_tokens_details"].get("cached_tokens") or 0)
    if not input_tokens and not output_tokens and not cached_tokens:
        # The provider omitted usage: never fabricate quantities or claim a
        # misleading zero — the cost is unknown.
        return None
    uncached_input = max(0, input_tokens - cached_tokens)
    cache_cost = 0.0
    if cached_tokens:
        if cache_read_price is None:
            return None  # cached usage without a cache price: unknown, not zero
        cache_cost = (cached_tokens * cache_read_price) / 1_000_000
    return (
        (uncached_input * float(input_price)) / 1_000_000
        + (output_tokens * float(output_price)) / 1_000_000
        + cache_cost
    )
