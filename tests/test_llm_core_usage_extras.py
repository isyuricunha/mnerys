"""Provider-reported cache/reasoning usage accounting in llm_core.

OpenAI/GLM/DeepSeek report prompt-cache hits and reasoning output in
different fields; Anthropic reports cache read/write. The normalized usage
event must preserve these numbers without ever estimating them.
"""
from src import llm_core
from src.llm_core import _normalize_usage_counts, _openai_usage_extras


def test_normalize_keeps_input_and_output_only_by_default():
    assert _normalize_usage_counts(1102, 350) == {
        "input_tokens": 1102,
        "output_tokens": 350,
    }


def test_openai_style_cached_and_reasoning_tokens_are_extracted():
    usage = {
        "prompt_tokens": 721_000,
        "completion_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 714_000},
        "completion_tokens_details": {"reasoning_tokens": 512},
    }
    extras = _openai_usage_extras(usage)
    normalized = _normalize_usage_counts(
        usage["prompt_tokens"], usage["completion_tokens"], extra=extras,
    )
    assert normalized["cached_tokens"] == 714_000
    assert normalized["reasoning_tokens"] == 512


def test_deepseek_cache_hit_alias_is_extracted():
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 200,
        "prompt_cache_hit_tokens": 4200,
    }
    normalized = _normalize_usage_counts(
        usage["prompt_tokens"], usage["completion_tokens"],
        extra=_openai_usage_extras(usage),
    )
    assert normalized["cached_tokens"] == 4200
    assert "reasoning_tokens" not in normalized


def test_anthropic_cache_read_and_write_are_preserved():
    normalized = _normalize_usage_counts(
        900, 120,
        extra={"cache_read_tokens": 850, "cache_creation_tokens": 40},
    )
    assert normalized["cache_read_tokens"] == 850
    assert normalized["cache_creation_tokens"] == 40


def test_malformed_extra_values_are_dropped():
    normalized = _normalize_usage_counts(
        100, 10,
        extra={
            "cached_tokens": "many",
            "cache_read_tokens": True,
            "cache_creation_tokens": -3,
            "reasoning_tokens": 1.5,
        },
    )
    assert set(normalized) == {"input_tokens", "output_tokens"}


def test_malformed_usage_returns_none():
    assert _normalize_usage_counts("many", 10) is None
    assert _normalize_usage_counts(10, float("inf")) is None


def _glm_style_usage_event(monkeypatch, usage_chunk):
    """Drive stream_llm with a canned final usage chunk; return usage data."""
    import asyncio
    import json as _json

    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: ' + _json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi"}}]})
            yield 'data: ' + _json.dumps({
                "choices": [],
                "usage": usage_chunk,
            })
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _Ctx:
        def __init__(self):
            self._resp = _Resp()

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, method, url, **kw):
            return _Ctx()

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        usage = None
        async for chunk in llm_core.stream_llm(
            "http://127.0.0.1:8081/v1/chat/completions",
            "glm-test",
            [{"role": "user", "content": "hi"}],
        ):
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln.startswith("data: ") and ln[6:] != "[DONE]":
                    try:
                        ev = _json.loads(ln[6:])
                    except ValueError:
                        continue
                    if ev.get("type") == "usage":
                        usage = ev["data"]
        return usage

    return asyncio.run(run())


def test_stream_usage_event_preserves_provider_cache_and_reasoning(monkeypatch):
    usage = _glm_style_usage_event(monkeypatch, {
        "prompt_tokens": 721_000,
        "completion_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 714_000},
        "completion_tokens_details": {"reasoning_tokens": 512},
    })
    assert usage is not None
    assert usage["input_tokens"] == 721_000
    assert usage["output_tokens"] == 900
    assert usage["cached_tokens"] == 714_000
    assert usage["reasoning_tokens"] == 512
