"""Owner-scoped foreground fallback in the direct admitted Agent composer.

All providers, preferences and endpoint discovery are synthetic. No live
requests, real credentials, production DB, or conversation writes.
"""
from dataclasses import replace
import json

import pytest

from src import foreground_model_routing as routing
from src import turn_service as turn_module
from src.foreground_model_routing import ForegroundModelPolicy
from src.gateway_admission import AdmissionDenied
from src.gateway_turn_composer import TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_turn_composer import fixture, gather

BACKUP = ("https://backup.example.com/v1", "backup-model", {
    "Authorization": "PRIVATE_BACKUP_FIXTURE",
})
EXCLUDED = ("https://disallowed.example.com/v1", "excluded-model", {
    "Authorization": "PRIVATE_DISALLOWED_FIXTURE",
})
PRIMARY_LABEL = {
    "endpoint_id": "primary", "endpoint_label": "Primary",
    "endpoint_cost_tracked": False,
}


def mock_owner_preferences(monkeypatch, *, enabled=True):
    owners = []
    resolved = []

    def prefs(owner):
        owners.append(owner)
        return {
            "foreground_fallback_enabled": enabled,
            "foreground_model_fallbacks": [
                {"endpoint_id": "backup", "model": "backup-model"},
                {"endpoint_id": "excluded", "model": "excluded-model"},
            ],
        }

    def provider(entries, *, owner, require_exact_model):
        assert require_exact_model
        resolved.append((owner, tuple(item["model"] for item in entries)))
        candidates = {"backup-model": BACKUP, "excluded-model": EXCLUDED}
        return [
            (candidates[item["model"]], {
                "endpoint_id": item["endpoint_id"],
                "endpoint_label": "Fallback " + item["model"],
                "endpoint_cost_tracked": False,
            })
            for item in entries
        ]

    monkeypatch.setattr(routing, "_load_policy_preferences", prefs)
    monkeypatch.setattr(routing, "resolve_fallback_entries_with_descriptors", provider)
    monkeypatch.setattr(
        routing, "resolve_route_descriptor",
        lambda *args, **kwargs: dict(PRIMARY_LABEL),
    )
    return owners, resolved


def enabled(settings=None):
    return settings or TrustedTextTurnSettings(
        enable_model_fallback=True,
        allowed_fallback_models=frozenset({"backup-model"}),
    )


@pytest.mark.asyncio
async def test_default_direct_profile_remains_strict_and_never_reads_fallback_prefs(
    monkeypatch,
):
    admission, session, ports, options = fixture()

    def forbid(*args, **kwargs):
        pytest.fail("Default direct profile must not read model fallback preferences")

    monkeypatch.setattr(routing, "_load_policy_preferences", forbid)
    observed = []
    async def loop(*args, **kwargs):
        observed.append(kwargs)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", loop)
    assert (await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **options,
    ))) == ["data: [DONE]\n\n"]
    assert "fallbacks" not in observed[0]
    assert "route_descriptors" not in observed[0]
    assert "defer_context_shaping" not in observed[0]
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("credential,expected_owner", [
    ("browser", "alice"),
    ("bearer", "alice"),
    ("single_user", None),
])
async def test_opted_in_fallback_passes_only_user_enabled_allowed_routes(
    monkeypatch, credential, expected_owner,
):
    owners, resolved = mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture(credential)
    options["settings"] = enabled()
    seen = []

    async def loop(url, model, messages, **kwargs):
        seen.append((url, model, messages, kwargs))
        yield 'data: {"delta":"Backup answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [event async for event in delivery.events]
    assert delivery.outcome.state == "completed"
    assert [event.type for event in events][-2:] == [
        "message.final", "turn.completed",
    ]
    assert owners == [expected_owner]
    assert resolved == [(expected_owner, ("backup-model",))]
    url, model, messages, runtime = seen[0]
    assert (url, model) == (session.endpoint_url, session.model)
    assert runtime["headers"] is session.headers
    assert runtime["fallbacks"] == [BACKUP]
    assert runtime["fallback_statuses"] == routing.FOREGROUND_AVAILABILITY_STATUSES
    assert runtime["fallback_on_empty"] is False
    assert runtime["defer_context_shaping"] is True
    assert runtime["route_descriptors"] == [
        PRIMARY_LABEL,
        {
            "endpoint_id": "backup",
            "endpoint_label": "Fallback backup-model",
            "endpoint_cost_tracked": False,
        },
    ]
    assert runtime["request_authority"].bound_to(
        owner=expected_owner, session_id=session.id,
    )
    assert "PRIVATE_BACKUP_FIXTURE" not in repr(events)
    assert "PRIVATE_DISALLOWED_FIXTURE" not in repr(events)
    assert EXCLUDED not in runtime["fallbacks"]
    assert not session.messages


@pytest.mark.asyncio
async def test_no_user_consent_stays_on_strict_route_even_when_adapter_opted_in(
    monkeypatch,
):
    owners, resolved = mock_owner_preferences(monkeypatch, enabled=False)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    received = []

    async def loop(_url, _model, _messages, **runtime):
        received.append(runtime)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", loop)
    await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
        admission, session, **options,
    ))
    assert owners == ["alice"]
    assert not resolved
    assert "fallbacks" not in received[0]
    assert not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"enable_model_fallback": "true"},
    {"enable_model_fallback": True},
    {"enable_model_fallback": True, "allowed_fallback_models": ["backup-model"]},
    {"enable_model_fallback": True, "allowed_fallback_models": frozenset({""})},
    {"enable_model_fallback": True, "allowed_fallback_models": frozenset({1})},
    {"enable_model_fallback": False, "allowed_fallback_models": frozenset({1})},
])
async def test_untrusted_fallback_allowance_rejected_before_context_io(
    monkeypatch, bad,
):
    admission, session, ports, options = fixture()
    options["settings"] = replace(options["settings"], **bad)

    def forbidden(*args, **kw):
        pytest.fail("Invalid server fallback settings reached model policy")

    monkeypatch.setattr(routing, "_load_policy_preferences", forbidden)
    with pytest.raises(AdmissionDenied):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **options,
        ))
    assert not ports.operations
    assert not session.messages


@pytest.mark.asyncio
async def test_account_denial_precedes_fallback_preference_or_endpoint_io(
    monkeypatch,
):
    admission, session, ports, options = fixture()
    options["settings"] = replace(
        enabled(), privileges={"can_use_agent": False},
    )

    def forbidden(*args, **kw):
        pytest.fail("Account-denied fallback profile performed preference I/O")

    monkeypatch.setattr(routing, "_load_policy_preferences", forbidden)
    with pytest.raises(AdmissionDenied):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **options,
        ))
    assert not ports.operations


@pytest.mark.asyncio
async def test_rejects_service_candidate_outside_server_allowance(
    monkeypatch,
):
    import src.gateway_turn_composer as composer
    mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    monkeypatch.setattr(
        composer, "build_admitted_foreground_routes",
        lambda *args, **kwargs: (
            [(session.endpoint_url, session.model, session.headers), EXCLUDED],
            [PRIMARY_LABEL, {"endpoint_id": "not-allowed"}],
        ),
    )
    with pytest.raises(AdmissionDenied, match="fallback route selection"):
        await gather(GatewayTurns(TurnService()).stream_composed_text_turn(
            admission, session, **options,
        ))
    assert not session.messages


@pytest.mark.asyncio
async def test_legacy_provider_fallback_runs_inside_same_existing_agent_loop(
    monkeypatch,
):
    import src.agent_loop as loop
    from src import llm_core

    mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    actual_routes = []

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)

    async def provider(url, model, messages, **kwargs):
        actual_routes.append((url, model))
        if model == "fixture-model":
            yield 'event: error\ndata: {"status": 503, "error": "synthetic outage"}\n\n'
        else:
            assert model == "backup-model"
            assert any(
                item.get("role") == "user"
                and "Explain how a queue works" in str(item.get("content"))
                for item in messages
            )
            yield 'data: {"delta":"Fallback answered."}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [event async for event in delivery.events]
    assert actual_routes == [
        (session.endpoint_url, session.model),
        (BACKUP[0], BACKUP[1]),
    ]
    assert delivery.outcome.state == "completed"
    assert delivery.outcome.content == "Fallback answered."
    routes = [event for event in events if event.type == "model.route.selected"]
    assert len(routes) == 1
    assert routes[0].payload == {
        "selected_model": "fixture-model",
        "answering_model": "backup-model",
        "candidate_index": 1,
        "round": 1,
    }
    assert routes[0].sequence < next(
        event.sequence for event in events if event.type == "message.delta"
    )
    assert "PRIVATE_BACKUP_FIXTURE" not in repr(events)
    assert not session.messages


@pytest.mark.asyncio
async def test_ineligible_provider_error_does_not_try_backup(monkeypatch):
    import src.agent_loop as loop
    from src import llm_core

    mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    actual_routes = []

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)

    async def provider(url, model, messages, **kwargs):
        actual_routes.append(model)
        yield 'event: error\ndata: {"status":401,"error":"synthetic refusal"}\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in delivery.events]
    assert actual_routes == ["fixture-model"]
    assert delivery.outcome.state == "failed"
    assert events[-1].type == "turn.failed"
    assert not any(item.type == "model.route.selected" for item in events)
    assert "synthetic refusal" not in repr(events)
    assert not session.messages


@pytest.mark.asyncio
async def test_visible_output_never_retries_on_another_provider(monkeypatch):
    import src.agent_loop as loop
    from src import llm_core

    mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    actual_routes = []

    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)

    async def provider(url, model, messages, **kwargs):
        actual_routes.append(model)
        yield 'data: {"delta":"Partial first route."}\n\n'
        yield 'event: error\ndata: {"status":503,"error":"synthetic outage"}\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", provider)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [item async for item in delivery.events]
    assert actual_routes == ["fixture-model"]
    assert delivery.outcome.state == "failed"
    assert events[-1].type == "turn.failed"
    assert not any(item.type == "model.route.selected" for item in events)
    assert "PRIVATE_BACKUP_FIXTURE" not in repr(events)
    assert not session.messages


@pytest.mark.asyncio
async def test_opted_in_fallback_cancellation_closes_the_original_loop(monkeypatch):
    from src import turn_service as runtime

    mock_owner_preferences(monkeypatch)
    admission, session, ports, options = fixture()
    options["settings"] = enabled()
    closed = []

    async def loop(*args, **kwargs):
        try:
            assert kwargs["fallbacks"] == [BACKUP]
            yield 'data: {"delta":"First route started"}\n\n'
            yield "data: [DONE]\n\n"
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime, "stream_agent_loop", loop)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    assert (await anext(delivery.events)).type == "message.delta"
    await delivery.events.aclose()
    assert closed == [True]
    assert delivery.outcome.state == "cancelled"
    assert not session.messages
