"""Provider-reported cache/reasoning usage accounting in llm_core.

OpenAI/GLM/DeepSeek report prompt-cache hits and reasoning output in
different fields; Anthropic reports cache read/write. The normalized usage
event must preserve these numbers without ever estimating them.
"""
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
