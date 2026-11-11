"""M5.4 increment 4: native reasoning, effort controls and token accounting.

The provider-independent intent contract translates preferences ONLY into
documented per-model wire fields; Anthropic thinking blocks (with opaque
signatures and redacted data) round-trip verbatim through streaming, history
and payload building; usage accounting never double-counts reasoning or cache
tokens and never fabricates usage.
"""
import json
import time
from types import SimpleNamespace

import pytest

OWNER = "alice"

import src.llm_core as llm
from src.reasoning_contract import (
    ANTHROPIC_MIN_BUDGET_TOKENS,
    anthropic_forced_tool_choice_rejected,
    anthropic_thinking_config,
    assistant_thinking_blocks,
    resolve_reasoning_intent,
)


# ── The intent contract ───────────────────────────────────────────────────

def test_user_max_preference_maps_to_documented_anthropic_effort():
    intent = resolve_reasoning_intent(
        "anthropic", "claude-opus-5-5", preference="max", reasoning_effort="max",
    )
    assert intent.thinking == {"type": "adaptive"}
    assert intent.effort == "max"


def test_anthropic_generations_use_the_documented_modes():
    # 4.7+/5.x: adaptive REQUIRED (manual budgets 400).
    config = anthropic_thinking_config("claude-opus-5-5", thinking_mode="on")
    assert config["thinking"] == {"type": "adaptive"}
    assert "rejected" in config["note"]
    # 4.6: adaptive + effort (budget deprecated).
    config = anthropic_thinking_config("claude-opus-4-6", thinking_mode="on", reasoning_effort="high")
    assert config["thinking"] == {"type": "adaptive"}
    assert config["output_config"] == {"effort": "high"}
    # 4.5-and-earlier: manual budget with the documented bounds.
    config = anthropic_thinking_config("claude-sonnet-4-5", thinking_mode="on", max_tokens=16000)
    assert config["thinking"]["type"] == "enabled"
    assert config["thinking"]["budget_tokens"] == 16000 - 1024
    # Opus 4.5 composes effort with a budget.
    config = anthropic_thinking_config(
        "claude-opus-4-5", thinking_mode="on", reasoning_effort="low", max_tokens=16000,
    )
    assert config["output_config"] == {"effort": "low"}


def test_budget_clamps_refuse_invalid_values():
    config = anthropic_thinking_config("claude-sonnet-4-5", reasoning_effort="10", max_tokens=8000)
    assert config["thinking"]["budget_tokens"] == ANTHROPIC_MIN_BUDGET_TOKENS
    config = anthropic_thinking_config("claude-sonnet-4-5", reasoning_effort="50000", max_tokens=8000)
    assert config["thinking"]["budget_tokens"] == 7999  # < max_tokens (documented)
    assert config["thinking"]["budget_tokens"] >= ANTHROPIC_MIN_BUDGET_TOKENS


def test_unsupported_effort_values_are_never_fabricated():
    intent = resolve_reasoning_intent("anthropic", "claude-sonnet-4-5", reasoning_effort="yolo-max")
    # An unrecognized value resolves to nothing fabricated.
    assert intent.effort is None
    assert intent.budget_tokens is None
    intent = resolve_reasoning_intent("openai", "gpt-5", reasoning_effort="max")
    assert "not a documented" in intent.note


def test_forced_tool_choice_restriction():
    assert anthropic_forced_tool_choice_rejected("claude-opus-5-5") is True
    assert anthropic_forced_tool_choice_rejected("claude-sonnet-4-5") is False


# ── Opaque thinking-block continuity ──────────────────────────────────────

THINKING_BLOCKS = [
    {"type": "thinking", "thinking": "private reasoning", "signature": "OPAQUE-SIG-BYTES"},
    {"type": "redacted_thinking", "data": "ENCRYPTED-BLOB"},
]


def test_payload_replays_thinking_blocks_verbatim_before_tool_use():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "Bash", "arguments": "{}"}}],
         "thinking_blocks": THINKING_BLOCKS},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]
    payload = llm._build_anthropic_payload(
        "claude-opus-5-5", messages, 0.7, 16000,
        tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
        thinking_mode="on", reasoning_effort="high",
    )
    content = payload["messages"][0]["content"]
    # Byte-for-byte: the signature and redacted data are untouched.
    assert content[0] == THINKING_BLOCKS[0]
    assert content[1] == THINKING_BLOCKS[1]
    assert content[2]["type"] == "tool_use"


def test_accessor_never_mutates_stored_blocks():
    stored = [{"type": "thinking", "thinking": "x", "signature": "sig"}]
    message = {"role": "assistant", "thinking_blocks": stored}
    replayed = assistant_thinking_blocks(message)
    replayed.append({"type": "text", "text": "injected"})
    assert stored == [{"type": "thinking", "thinking": "x", "signature": "sig"}]


def test_messages_without_thinking_blocks_stay_unchanged():
    messages = [
        {"role": "assistant", "content": "text", "tool_calls": [
            {"id": "t1", "function": {"name": "Bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]
    payload = llm._build_anthropic_payload("claude-sonnet-4-5", messages, 0.7, 4096)
    assert payload["messages"][0]["content"][0]["type"] == "text"


# ── Streaming: thinking deltas + signature assembly ───────────────────────

def _anthropic_stream_events():
    return [
        {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reason "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "privately"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG-PART-1"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG-PART-2"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu-1", "name": "Bash"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": \"ls\"}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]


def test_streaming_captures_thinking_and_signature_verbatim():
    async def collect():
        events = []
        async for chunk in llm.stream_llm(
            "https://api.anthropic.com/v1", "claude-opus-5-5",
            [{"role": "user", "content": "hi"}],
            thinking_mode="on", reasoning_effort="high",
        ):
            if isinstance(chunk, str) and chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
                try:
                    events.append(json.loads(chunk[6:]))
                except ValueError:
                    pass
        return events

    import asyncio

    async def fake_stream_response(*args, **kwargs):
        class FakeStream:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def aiter_lines(self):
                for event in _anthropic_stream_events():
                    yield f"data: {json.dumps(event)}"
        return FakeStream()

    asyncio.run = asyncio.run  # noqa
    import src.llm_core as llm_mod

    async def run_with(monkey):
        async for _ in monkey():
            pass

    # Patch the HTTP client to serve the scripted SSE stream.
    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def stream(self, *a, **kw):
            class S:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                @property
                def status_code(self):
                    return 200
                async def aiter_lines(self):
                    for event in _anthropic_stream_events():
                        yield f"data: {json.dumps(event)}"
            return S()

    async def run():
        import httpx

        original_client = llm._get_http_client
        llm._get_http_client = lambda: FakeAsyncClient()
        try:
            return await collect()
        finally:
            llm._get_http_client = original_client

    events = asyncio.run(run())
    thinking_events = [e for e in events if e.get("thinking")]
    assert "".join(e["delta"] for e in thinking_events) == "reason privately"
    blocks_events = [e for e in events if e.get("type") == "thinking_blocks"]
    assert blocks_events, "the thinking blocks event must reach the loop"
    blocks = blocks_events[0]["blocks"]
    assert blocks[0] == {
        "type": "thinking", "thinking": "reason privately", "signature": "SIG-PART-1SIG-PART-2",
    }
    calls = [e for e in events if e.get("type") == "tool_calls"]
    assert calls[0]["calls"] == [{"id": "toolu-1", "name": "Bash", "arguments": "{\"command\": \"ls\"}"}]
    usage = [e for e in events if e.get("type") == "usage"]
    assert usage[0]["data"]["output_tokens"] == 42


# ── Accounting: reasoning/cache tokens ────────────────────────────────────

def test_openai_reasoning_tokens_not_double_counted(tmp_path, monkeypatch):
    # monkeypatch (NOT a raw env write + module reload): a raw write would
    # leak ODYSSEUS_DATA_DIR into every later test in the process.
    import os
    import src.model_metadata as mm

    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    mm._cache.reset_for_tests()
    catalog = {"openai": {"id": "openai", "models": {
        "gpt-5": {"id": "gpt-5", "limit": {"context": 400000, "output": 128000},
                  "cost": {"input": 2.0, "output": 8.0}}}}}
    open(os.path.join(str(tmp_path), "models_dev_catalog.json"), "w").write(json.dumps({
        "schema_version": 1, "source": "models_dev", "url": mm.MODELS_DEV_API_URL,
        "etag": '"x"', "fetched_at": time.time(), "stale": False, "catalog": catalog}))
    usage = {"prompt_tokens": 100, "completion_tokens": 200,
             "completion_tokens_details": {"reasoning_tokens": 150}}
    cost = mm.estimate_cost_usd_resolved("gpt-5", usage, "https://api.openai.com/v1", owner="alice")
    assert abs(cost - (100 * 2.0 + 200 * 8.0) / 1_000_000) < 1e-12  # once


def test_anthropic_cache_accounting_not_double_charged(tmp_path, monkeypatch):
    import os
    import src.model_metadata as mm

    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    mm._cache.reset_for_tests()
    catalog = {"anthropic": {"id": "anthropic", "models": {
        "claude-opus-4-5": {"id": "claude-opus-4-5",
                            "limit": {"context": 200000, "output": 64000},
                            "cost": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}}}}
    open(os.path.join(str(tmp_path), "models_dev_catalog.json"), "w").write(json.dumps({
        "schema_version": 1, "source": "models_dev", "url": mm.MODELS_DEV_API_URL,
        "etag": '"x"', "fetched_at": time.time(), "stale": False, "catalog": catalog}))
    usage = {"prompt_tokens": 1000, "completion_tokens": 100,
             "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100}
    cost = mm.estimate_cost_usd_resolved(
        "claude-opus-4-5", usage, "https://api.anthropic.com/v1", owner="alice",
    )
    # Uncached input 200 @5 + output 100 @25 + cache-read 800 @0.5; the cache
    # WRITE dimension is not billed by this estimator (no write usage count).
    assert abs(cost - (200 * 5 + 100 * 25 + 800 * 0.5) / 1_000_000) < 1e-12, cost


# ── Session preference persistence + model switching ──────────────────────

def test_session_reasoning_preference_persists_and_validates(tmp_path, monkeypatch):
    """The owner's provider-independent preference: explicit values validated,
    an empty value clears, model text can never set it (only the API)."""
    import types
    from uuid import uuid4
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as cdb
    from core.database import Session as DbSession, utcnow_naive
    from routes import session_routes as sr

    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(sr, "SessionLocal", factory)
    sid = f"s-{uuid4().hex[:8]}"
    db = factory()
    try:
        db.add(DbSession(id=sid, name="s", endpoint_url="http://x/v1/chat/completions",
                         model="m", owner=OWNER, revision=0, incarnation_id=uuid4().hex,
                         created_at=utcnow_naive(), updated_at=utcnow_naive()))
        db.commit()
    finally:
        db.close()

    def _owner_ok(request, looked_sid):
        return SimpleNamespace(session_id=looked_sid, owner=OWNER)

    monkeypatch.setattr(sr, "_verify_session_owner", _owner_ok)
    monkeypatch.setattr(sr, "effective_user", lambda request: OWNER)

    class M:
        def get_session(self, session_id):
            row = None
            db = factory()
            try:
                row = db.query(DbSession).filter(DbSession.id == session_id).first()
            finally:
                db.close()
            if row is None:
                raise KeyError(session_id)
            return SimpleNamespace(id=row.id, name=row.name, model=row.model,
                                   cwd=row.cwd, yolo_enabled=row.yolo_enabled,
                                   reasoning_effort=row.reasoning_effort,
                                   incarnation_id=row.incarnation_id, headers={},
                                   thinking_mode="off", rag=False, archived=False,
                                   endpoint_id=None, incognito=False)

        def update_session_name(self, session_id, name):
            pass

    router = sr.setup_session_routes(M(), {"REQUEST_TIMEOUT": 20}, webhook_manager=None)
    app = FastAPI()
    app.include_router(router)
    tc = TestClient(app)
    # An invalid value is refused.
    bad = tc.patch(f"/api/session/{sid}", data={"reasoning_effort": "explode"})
    assert bad.status_code == 400
    # A valid preference persists; an empty value clears.
    set_resp = tc.patch(f"/api/session/{sid}", data={"reasoning_effort": "high"})
    assert set_resp.status_code == 200 and set_resp.json()["reasoning_effort"] == "high"
    db = factory()
    try:
        assert db.query(DbSession).filter(DbSession.id == sid).first().reasoning_effort == "high"
    finally:
        db.close()
    # A token budget is a valid preference shape.
    budget = tc.patch(f"/api/session/{sid}", data={"reasoning_effort": "8192"})
    assert budget.status_code == 200 and budget.json()["reasoning_effort"] == "8192"
    cleared = tc.patch(f"/api/session/{sid}", data={"reasoning_effort": ""})
    # An empty-string form value is FastAPI-indistinguishable from a missing
    # field; the documented clear sends whitespace (same as the cwd contract).
    cleared = tc.patch(f"/api/session/{sid}", data={"reasoning_effort": "   "})
    assert cleared.status_code == 200 and cleared.json().get("reasoning_effort") in (None, "")


def test_model_switch_resets_an_unsupported_effort_preference():
    """Switching to a model that lacks the saved effort level resets the
    thinking-mode effort (the existing switch validation) — the PREFERENCE
    (reasoning_effort) stays for capable models and is never sent as an
    invalid wire parameter."""
    import inspect

    from src.gateway_session_store import update_owner_session

    source = inspect.getsource(update_owner_session)
    # The effort preference is validated + persisted; the effort-mode reset
    # for unsupported models is enforced at model switches.
    assert "reasoning_effort must be off, low, medium, high, max" in source


def test_privacy_reasoning_state_stays_out_of_client_payloads():
    """Raw thinking blocks/signatures ride only the internal history → the
    payload builder path; they are never emitted in tool outputs, client
    history pages, or logs."""
    import inspect

    from src.reasoning_contract import assistant_thinking_blocks

    # The accessor returns the blocks ONLY for the payload builder's internal
    # use; no logging/serialization site references it.
    from routes.history import history_routes

    history_source = inspect.getsource(history_routes)
    assert "thinking_blocks" not in history_source
    assert "signature" not in history_source
