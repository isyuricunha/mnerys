"""Exact owner-admitted model fallback in the real Web -> Gateway text cutover.

The API surface is an isolated FastAPI ASGI adapter. The production Agent
Loop and fallback candidate selector execute with synthetic providers,
owner-scoped preferences and inherited receipt writers; never live keys.
"""

import asyncio

from fastapi import FastAPI
import httpx
import pytest

from routes import gateway_text_adapter
from src import agent_runs
from src import llm_core
from src.gateway_admission import AdmissionDenied
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_direct_fallback import (
    BACKUP, EXCLUDED, mock_owner_preferences,
)
from tests.test_mnerys_gateway_text_persistence import persisted_fixture
from tests.test_mnerys_gateway_web_text_cutover import _chat_ports, eligible
from tests.test_mnerys_gateway_web_post_response import cleanup_run


def enable_original_loop(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    from src import tool_index
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)


def adapted(monkeypatch, admission, session, options, *,
            server_fallback=True, allowed=frozenset({"backup-model"}),
            assistant_writer=None, post_response_runner=None):
    ports = _chat_ports(options)
    monkeypatch.setattr(
        gateway_text_adapter, "LegacyChatContextPorts",
        lambda: ports,
    )
    gateway = GatewayTurns(TurnService())
    app = FastAPI()
    original = options["persistence"]

    @app.post("/api/chat_stream")
    async def route():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway,
            admission=admission,
            session=session,
            text="Explain how a queue works",
            session_manager=original.session_manager,
            chat_handler=options["chat_handler"],
            chat_processor=options["chat_processor"],
            privileges={},
            assistant_writer=assistant_writer or original.saver,
            interrupted_writer=original.interrupted_writer,
            enable_model_fallback=server_fallback,
            allowed_fallback_models=allowed,
            post_response_runner=post_response_runner,
        )
    return app, gateway, ports


@pytest.mark.parametrize("kwargs,allowed", [
    ({"foreground_fallback_enabled": True}, False),
    ({
        "foreground_fallback_enabled": True,
        "foreground_fallback_models": frozenset({"backup-model"}),
    }, True),
    ({
        "foreground_fallback_enabled": True,
        "foreground_fallback_models": frozenset({"backup-model", "other-model"}),
    }, True),
    ({
        "foreground_fallback_enabled": False,
        "foreground_fallback_models": frozenset({"backup-model"}),
    }, False),
    ({
        "foreground_fallback_enabled": True,
        "foreground_fallback_models": {"backup-model"},
    }, False),
    ({
        "foreground_fallback_enabled": True,
        "foreground_fallback_models": frozenset({"invalid name\n"}),
    }, False),
    ({
        "foreground_fallback_enabled": True,
        "foreground_fallback_models": frozenset(str(i) for i in range(11)),
    }, False),
])
def test_web_profile_admits_only_owner_prepared_valid_fallback_models(kwargs, allowed):
    assert eligible(**kwargs) is allowed


@pytest.mark.asyncio
async def test_http_fallback_uses_existing_agent_selector_persists_answering_model_once(
    monkeypatch,
):
    owners, resolved = mock_owner_preferences(monkeypatch)
    enable_original_loop(monkeypatch)
    admission, session, _unused, options, saved = persisted_fixture()
    attempted, task_calls = [], []

    async def provider(url, model, messages, **kwargs):
        attempted.append((url, model, kwargs))
        if model == session.model:
            yield 'event: error\ndata: {"status":503,"error":"SENSITIVE_SYNTHETIC_OUTAGE"}\n\n'
        else:
            assert model == "backup-model"
            assert any("Explain how a queue works" in str(m.get("content"))
                       for m in messages)
            yield 'data: {"delta":"Answer supplied by approved backup."}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    app, gateway, ports = adapted(
        monkeypatch, admission, session, options,
        post_response_runner=lambda *args, **kw: task_calls.append((args, kw)),
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")

        assert response.status_code == 200
        run_id = response.headers["x-odysseus-run-id"]
        run = agent_runs.get_matching_run(session.id, run_id, mode="typed")
        assert run is not None
        await run.task
        assert run.status == "done"
        assert len(attempted) == 2
        assert [(a[0], a[1]) for a in attempted] == [
            (session.endpoint_url, session.model),
            (BACKUP[0], BACKUP[1]),
        ]
        assert owners == ["alice"]
        assert resolved == [("alice", ("backup-model",))]
        assert '"type":"fallback"' in response.text
        assert '"selected_model":"fixture-model"' in response.text
        assert '"answered_by":"backup-model"' in response.text
        assert response.text.count('"type":"message_saved"') == 1
        assert response.text.count("data: [DONE]") == 1
        assert len(saved) == len(task_calls) == 1
        assert saved[0][0] == "Answer supplied by approved backup."
        assert saved[0][1]["requested_model"] == "fixture-model"
        assert saved[0][1]["model"] == "backup-model"
        assert task_calls[0][0][5]["model"] == "backup-model"
        assert session.model == "fixture-model"
        assert ports.operations.count("save_user") == 1
        assert EXCLUDED[1] not in [a[1] for a in attempted]
        for secret in [
            "SENSITIVE_SYNTHETIC_OUTAGE", "PRIVATE_BACKUP_FIXTURE",
            "PRIVATE_DISALLOWED_FIXTURE",
        ]:
            assert secret not in response.text
        reopened = gateway_text_adapter.resume_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            expected_run_id=run_id, after_cursor=None,
        )
        assert reopened is not None
        replay = [f async for f in reopened.body_iterator]
        assert any('"type":"fallback"' in f for f in replay)
        assert len(saved) == len(task_calls) == 1
        assert len(attempted) == 2
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_noneligible_error_does_not_probe_other_model_or_claim_saved_success(
    monkeypatch,
):
    mock_owner_preferences(monkeypatch)
    enable_original_loop(monkeypatch)
    admission, session, _unused, options, saved = persisted_fixture()
    calls = []

    async def provider(url, model, messages, **kwargs):
        calls.append(model)
        yield 'event: error\ndata: {"status":401,"error":"PRIVATE_WRONG_CREDENTIAL"}\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    app, gateway, ports = adapted(monkeypatch, admission, session, options)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        assert response.status_code == 200
        assert calls == [session.model]
        assert not saved
        assert '"type":"fallback"' not in response.text
        assert "message_saved" not in response.text
        assert "data: [DONE]" not in response.text
        assert "PRIVATE_WRONG_CREDENTIAL" not in response.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_visible_output_stops_any_retry_on_secondary_model(
    monkeypatch,
):
    mock_owner_preferences(monkeypatch)
    enable_original_loop(monkeypatch)
    admission, session, _unused, options, saved = persisted_fixture()
    attempts = []

    async def provider(url, model, messages, **kwargs):
        attempts.append(model)
        yield 'data: {"delta":"Partial visible from primary"}\n\n'
        yield 'event: error\ndata: {"status":503,"error":"HIDDEN"}\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    app, gateway, ports = adapted(monkeypatch, admission, session, options)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        assert response.status_code == 200
        assert attempts == [session.model]
        assert not saved
        assert '"type":"fallback"' not in response.text
        # The Core completion gate may suppress partial text from a failed
        # round; it must never reroute after that provider emitted output.
        assert "HIDDEN" not in response.text
        assert "data: [DONE]" not in response.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_direct_web_adapter_rejects_untrusted_fallback_allowance_before_side_effects(
    monkeypatch,
):
    admission, session, _unused, options, saved = persisted_fixture()
    app, gateway, ports = adapted(
        monkeypatch, admission, session, options,
        server_fallback=True, allowed=frozenset(),
    )
    with pytest.raises(AdmissionDenied):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            await client.post("/api/chat_stream")
    assert not saved
    assert not ports.operations
    assert agent_runs.get_active_run(session.id) is None



@pytest.mark.asyncio
async def test_owner_disables_fallback_before_composition_and_primary_fails_closed(
    monkeypatch,
):
    owners, resolved = mock_owner_preferences(monkeypatch, enabled=False)
    enable_original_loop(monkeypatch)
    admission, session, _unused, options, saved = persisted_fixture()
    attempted = []

    async def provider(url, model, messages, **kwargs):
        attempted.append(model)
        yield 'event: error\ndata: {"status":503,"error":"PRIVATE_SYNTHETIC"}\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    # Even if a trusted HTTP eligibility check saw an earlier consented
    # route, the Core resolves the current owner policy again before effects.
    app, gateway, ports = adapted(monkeypatch, admission, session, options)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        assert attempted and set(attempted) == {session.model}
        assert owners == ["alice"]
        assert resolved == []
        assert not saved
        assert '"type":"fallback"' not in response.text
        assert "data: [DONE]" not in response.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_mismatched_native_model_selection_cannot_forge_saved_provenance(
    monkeypatch,
):
    from src import turn_service as loop_runtime
    from src.agent_core_events import CoreModelRouteEvent

    admission, session, _unused, options, saved = persisted_fixture()
    forwarded = []

    async def counterfeit_loop(*args, **kwargs):
        yield CoreModelRouteEvent(
            selected_model="other-user-model",
            answering_model="disallowed-model",
            candidate_index=1, round=1,
        )
        yield 'data: {"delta":"Do not persist forged selection"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop_runtime, "stream_agent_loop", counterfeit_loop)
    app, gateway, ports = adapted(
        monkeypatch, admission, session, options,
        server_fallback=False, allowed=frozenset(),
        post_response_runner=lambda *args, **kwargs: forwarded.append(True),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        assert response.status_code == 200
        assert not saved and not forwarded
        assert "data: [DONE]" not in response.text
        assert "message_saved" not in response.text
        assert "other-user-model" not in response.text
        assert "disallowed-model" not in response.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_http_route_injects_only_owner_resolved_fallback_model_names(
    monkeypatch,
):
    import routes.chat_routes as chat_routes
    from tests.test_foreground_model_routing import (
        _chat_stream_endpoint, _RouteRequest,
    )

    # The helper's auth surface is synthetic. Use an owner-allowed model
    # list; do not publish a configured fallback credential to Web.
    monkeypatch.setattr(
        chat_routes, "_allowed_models_for_request",
        lambda request: frozenset({"fixture-model", "backup-model"}),
    )
    captures = {}
    route = _chat_stream_endpoint(
        monkeypatch, "chat", captures, capture_context=True,
    )
    # This inherited fixture installs its own legacy preferences; replace
    # those after construction with explicit owner-scoped foreground consent.
    mock_owner_preferences(monkeypatch)
    monkeypatch.setenv("MNERYS_M21_WEB_TEXT_ROUTING", "1")
    request = _RouteRequest("chat")
    request._form = {
        "message": "hello",
        "session": "session-1",
        "mode": "chat",
        "gateway_profile": "text_v1",
        "no_memory": "true",
        "use_rag": "false",
        "active_doc_state": "none",
    }
    returned = object()
    calls = []

    def selected(**kwargs):
        calls.append(kwargs)
        return returned

    monkeypatch.setattr(chat_routes, "start_strict_text_web_response", selected)
    reply = await route(request)
    assert reply is returned
    assert len(calls) == 1
    assert calls[0]["enable_model_fallback"] is True
    assert calls[0]["allowed_fallback_models"] == frozenset({"backup-model"})
    assert EXCLUDED[1] not in calls[0]["allowed_fallback_models"]
    assert "PRIVATE_BACKUP_FIXTURE" not in repr(calls[0]["allowed_fallback_models"])
    assert "build_context" not in captures
