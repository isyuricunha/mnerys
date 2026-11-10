"""Tests for _normalize_mistral_content() — Mistral's structured content parser.

Mistral's chat completions API returns content as a typed array when reasoning
is enabled, instead of the plain string most OpenAI-compat servers use:

    "content": [
      {"type": "thinking", "thinking": [{"type": "text", "text": "..."}], "closed": true},
      {"type": "text", "text": "..."}
    ]

_normalize_mistral_content() splits that into (text, thinking) plain strings.
The function is called from three sites:
  - llm_call (sync, non-streaming response parser)
  - llm_call_async (async, non-streaming response parser)
  - stream_llm (streaming delta parser)

These tests pin the contract: string passthrough, the array shape, and the
edge cases (empty, garbage, missing fields) so a refactor doesn't silently
drop thinking content or break non-Mistral providers.
"""
from src.llm_core import _normalize_mistral_content
import httpx
import pytest


def test_provider_reasoning_replay_uses_documented_message_content_chunks():
    from src.llm_core import _provider_reasoning_messages
    original = [
        {"role": "assistant", "content": None, "reasoning_content": "reasoned",
         "tool_calls": [{"id": "call", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call", "content": "real result"},
    ]
    adapted = _provider_reasoning_messages(original, "mistral")
    assert "reasoning_content" not in adapted[0]
    assert adapted[0]["content"] == [{"type": "thinking", "thinking": [{"type": "text", "text": "reasoned"}], "closed": True}]
    assert adapted[0]["tool_calls"] == original[0]["tool_calls"]
    assert adapted[1] == original[1]
    assert original[0]["reasoning_content"] == "reasoned"
    assert _provider_reasoning_messages(original, "openai") == original


def test_provider_reasoning_replay_keeps_assistant_text_and_existing_chunks():
    from src.llm_core import _provider_reasoning_messages
    messages = [
        {"role": "assistant", "content": "visible", "reasoning_content": "private"},
        {"role": "assistant", "content": [{"type": "text", "text": "second"}], "reasoning_content": "more"},
    ]
    adapted = _provider_reasoning_messages(messages, "mistral")
    assert adapted[0]["content"][-1] == {"type": "text", "text": "visible"}
    assert adapted[1]["content"][-1] == {"type": "text", "text": "second"}
    assert all("reasoning_content" not in message for message in adapted)


def test_provider_reasoning_replay_does_not_duplicate_existing_thinking():
    from src.llm_core import _provider_reasoning_messages
    chunks = [{"type": "thinking", "thinking": [{"type": "text", "text": "original"}], "closed": True},
              {"type": "text", "text": "answer"}]
    result = _provider_reasoning_messages([
        {"role": "assistant", "content": chunks, "reasoning_content": "original"},
    ], "mistral")
    assert result[0]["content"] == chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
async def test_provider_wire_request_encodes_reasoning_for_all_call_paths(monkeypatch, mode):
    import json
    from src import llm_core
    captured = []
    messages = [
        {"role": "user", "content": "Perform the action"},
        {"role": "assistant", "content": "Working", "reasoning_content": "prior thinking"},
        {"role": "user", "content": "Continue"},
    ]

    def response(request):
        captured.append(json.loads(request.content))
        if mode == "stream":
            return httpx.Response(200, request=request, text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
        monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *args: None)
        monkeypatch.setattr(llm_core, "note_model_activity", lambda *args, **kw: None)
        if mode == "sync":
            def post(url, **kwargs):
                return response(httpx.Request("POST", url, json=kwargs["json"]))
            monkeypatch.setattr(llm_core.httpx, "post", post)
            llm_core._response_cache.clear()
            llm_core.llm_call("https://api.mistral.ai/v1", "mistral-small-latest", messages)
        elif mode == "async":
            await llm_core.llm_call_async("https://api.mistral.ai/v1", "mistral-small-latest", messages)
        else:
            _ = [event async for event in llm_core.stream_llm("https://api.mistral.ai/v1", "mistral-small-latest", messages)]
    assistant = next(m for m in captured[0]["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in assistant
    assert assistant["content"][0]["thinking"][0]["text"] == "prior thinking"
    assert assistant["content"][1] == {"type": "text", "text": "Working"}


def test_string_passthrough_returns_text_with_empty_thinking():
    """Plain string content (the common case) passes through unchanged."""
    text, thinking = _normalize_mistral_content("hello world")
    assert text == "hello world"
    assert thinking == ""


def test_empty_string_passthrough():
    text, thinking = _normalize_mistral_content("")
    assert text == ""
    assert thinking == ""


def test_array_with_thinking_and_text_blocks():
    """Mistral's documented format: thinking block + text block."""
    content = [
        {
            "type": "thinking",
            "thinking": [{"type": "text", "text": "Let me work through this..."}],
            "closed": True,
        },
        {"type": "text", "text": "The answer is 42."},
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == "The answer is 42."
    assert thinking == "Let me work through this..."


def test_array_with_only_thinking_block():
    """Streaming deltas often contain only a thinking fragment (no text block yet)."""
    content = [
        {
            "type": "thinking",
            "thinking": [{"type": "text", "text": "Okay, let's"}],
            "closed": True,
        }
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == ""
    assert thinking == "Okay, let's"


def test_array_with_only_text_block():
    """Final answer delta — only the text block, no thinking."""
    content = [{"type": "text", "text": "Final answer."}]
    text, thinking = _normalize_mistral_content(content)
    assert text == "Final answer."
    assert thinking == ""


def test_array_concatenates_multiple_text_blocks():
    """Multiple text blocks are concatenated in order."""
    content = [
        {"type": "text", "text": "part 1 "},
        {"type": "text", "text": "part 2"},
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == "part 1 part 2"


def test_array_concatenates_multiple_thinking_fragments():
    """Multiple thinking sub-blocks are concatenated in order."""
    content = [
        {
            "type": "thinking",
            "thinking": [
                {"type": "text", "text": "first "},
                {"type": "text", "text": "second"},
            ],
            "closed": True,
        }
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == ""
    assert thinking == "first second"


def test_empty_array_returns_empty_strings():
    text, thinking = _normalize_mistral_content([])
    assert text == ""
    assert thinking == ""


def test_array_with_garbage_entries_skips_them():
    """Non-dict entries, missing type, missing text — all silently skipped."""
    content = [
        "not a dict",
        None,
        {"type": "unknown_type", "text": "should be ignored"},
        {"type": "text"},  # missing text key
        {"type": "thinking"},  # missing thinking key
        {"type": "text", "text": "valid text"},
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == "valid text"
    assert thinking == ""


def test_none_returns_empty_strings():
    """Defensive: None content (server bug or schema drift) doesn't crash."""
    text, thinking = _normalize_mistral_content(None)
    assert text == ""
    assert thinking == ""


def test_int_returns_empty_strings():
    """Defensive: wrong-typed content doesn't crash."""
    text, thinking = _normalize_mistral_content(42)
    assert text == ""
    assert thinking == ""


def test_thinking_block_with_string_inner():
    """Some Mistral API versions may use a string instead of an array for
    the inner 'thinking' field. Accept both shapes."""
    content = [
        {"type": "thinking", "thinking": "inline string thinking"},
        {"type": "text", "text": "answer"},
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == "answer"
    assert thinking == "inline string thinking"


def test_thinking_block_with_empty_text_field():
    """Empty text fields don't pollute the output."""
    content = [
        {"type": "thinking", "thinking": [{"type": "text", "text": ""}], "closed": True},
        {"type": "text", "text": ""},
    ]
    text, thinking = _normalize_mistral_content(content)
    assert text == ""
    assert thinking == ""
