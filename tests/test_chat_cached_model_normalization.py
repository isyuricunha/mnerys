"""Cached model ID behavior across the transport-independent context seam."""

from types import SimpleNamespace

import pytest

from routes import chat_helpers


def _session():
    return SimpleNamespace(
        endpoint_url="https://example.com/v1",
        model="local-alias",
        owner="alice",
    )


def test_chat_context_uses_cached_models_before_live_model_probe(monkeypatch):
    """The legacy provider resolves the cache before calling the live API."""
    calls = []

    def cached(session):
        calls.append(("cache", session.model))
        return "provider/local-alias"

    def live(*_args, **_kwargs):
        pytest.fail("Live model listing was called despite a cache match")

    monkeypatch.setattr(chat_helpers, "_normalize_model_id_from_cache", cached)
    monkeypatch.setattr(chat_helpers, "normalize_model_id", live)

    assert chat_helpers.LegacyChatContextPorts().normalize_model(_session()) == (
        "provider/local-alias"
    )
    assert calls == [("cache", "local-alias")]


def test_cached_model_match_keeps_basename_normalization():
    """Preserve exact-match precedence and path basename fallback."""
    match = chat_helpers._match_cached_model_id
    models = ["org/other-model", "provider/local-alias", "org/local-alias"]
    assert match("provider/local-alias", models) == "provider/local-alias"
    assert match("local-alias", models) == "provider/local-alias"
    assert match("nested/local-alias/", models) == "provider/local-alias"
    assert match("missing", models) is None
    assert match("", models) is None
