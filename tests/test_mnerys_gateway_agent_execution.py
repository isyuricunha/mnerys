"""M2.1 functional in-process Gateway -> existing Agent Loop acceptance.

No FastAPI router, real model provider, production DB or external tools are used.
These tests exercise the actual TurnService bridge/contract lifecycle with
the model loop substituted by a deterministic synthetic provider.
"""

import ast
import asyncio
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import turn_service as turn_service_module
from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
)
from src.gateway_turns import GatewayTurns
from src.tool_execution import (
    AgentExecutionBridge, bind_execution_bridge, get_active_execution_bridge,
)
from src.tool_policy import build_effective_tool_policy
from src.turn_contract import active_turn_contract, bind_turn_contract
from src.turn_service import TurnService


def setup(*, owner="alice", credential="browser", workspace=None, disabled=()):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    admission = GatewayAdmission().admit(
        principal, "session-1",
        lambda _: SessionOwnership(owner_id=owner),
    )
    session = SimpleNamespace(
        id="session-1", owner=principal.owner_id,
        endpoint_url="https://example.com/api",
        model="test-model",
        headers={"Authorization": "placeholder"},
    )
    policy = build_effective_tool_policy(
        disabled_tools=set(disabled), last_user_message="Hello",
    )
    authority = RequestAuthority.empty(
        owner=principal.owner_id,
        session_id=admission.session_id,
        workspace=workspace,
    ).restrict(policy)
    context = dict(
        context_owner=principal.owner_id,
        bridge=None,
        endpoint_url=session.endpoint_url,
        model=session.model,
        messages=[{"role": "user", "content": "Hello"}],
        authority=authority,
        policy=policy,
        workspace=workspace,
        turn_contract=None,
        max_rounds=2,
    )
    return admission, session, context


async def collect(stream):
    return [item async for item in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
async def test_gateway_directly_starts_existing_agent_loop_without_web_router(
    monkeypatch, credential,
):
    admission, session, kw = setup(
        credential=credential,
        disabled={"bash", "web_search"},
    )
    received = []

    async def fake_model_loop(*args, **options):
        received.append((args, options))
        yield 'data: {"type": "delta", "delta": "Hello"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_model_loop)
    gateway = GatewayTurns(TurnService())
    data = await collect(gateway.stream_admitted_agent(admission, session, **kw))
    assert data == [
        'data: {"type": "delta", "delta": "Hello"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert len(received) == 1
    args, passed = received[0]
    assert args == (session.endpoint_url, session.model, kw["messages"])
    assert passed["session_id"] == admission.session_id
    assert passed["history_session"] is session
    assert passed["headers"] is session.headers
    assert passed["owner"] == admission.principal.owner_id
    assert passed["delegated_credential"] is admission.principal.delegated
    assert passed["request_authority"].bound_to(
        owner=admission.principal.owner_id,
        session_id=admission.session_id,
    )
    assert {"bash", "web_search"} <= set(passed["disabled_tools"])
    assert passed["tool_policy"] is kw["policy"]
    assert passed["turn_contract"] is None
    assert passed["max_rounds"] == 2
    assert passed["native_tool_catalog"] is True


@pytest.mark.asyncio
async def test_client_cannot_override_server_native_catalog_flag():
    admission, session, options = setup()
    options["native_tool_catalog"] = False
    with pytest.raises(AdmissionDenied, match="Protected agent execution field"):
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **options,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "close", "error", "cancel"])
async def test_gateway_preserves_tool_bridge_and_contract_lifetime(
    monkeypatch, ending,
):
    admission, session, kw = setup()
    checked = []
    contract = object()
    outer_contract = object()

    async def unused_tool(*_args, **_kwargs):
        return "no execution", {}

    inner_bridge = AgentExecutionBridge(
        unused_tool, frozenset({"web_search"}), name="inner",
    )
    outer_bridge = AgentExecutionBridge(
        unused_tool, frozenset({"bash"}), name="outer",
    )
    kw["bridge"] = inner_bridge
    kw["turn_contract"] = contract

    async def fake_loop(*_args, **options):
        assert active_turn_contract() is contract
        assert get_active_execution_bridge() is inner_bridge
        assert options["turn_contract"] is contract
        try:
            yield "chunk"
            if ending == "error":
                raise RuntimeError("provider error")
            if ending == "cancel":
                raise asyncio.CancelledError()
        finally:
            checked.append(
                (active_turn_contract(), get_active_execution_bridge()),
            )

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_loop)
    with bind_turn_contract(outer_contract), bind_execution_bridge(outer_bridge):
        stream = GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        )
        assert await anext(stream) == "chunk"
        if ending == "close":
            await stream.aclose()
        else:
            with pytest.raises({
                "complete": StopAsyncIteration,
                "error": RuntimeError,
                "cancel": asyncio.CancelledError,
            }[ending]):
                await anext(stream)
        assert active_turn_contract() is outer_contract
        assert get_active_execution_bridge() is outer_bridge
        await stream.aclose()

    assert checked == [(contract, inner_bridge)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id", "principal",
    "loaded_session", "loaded_owner", "context_owner",
])
async def test_invalid_or_foreign_admission_denied_before_agent_call(
    monkeypatch, tamper,
):
    admission, session, kw = setup()

    async def unexpected(*_args, **_options):
        raise AssertionError("Untrusted turn reached model execution")
        yield "never"

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", unexpected)
    if tamper == "missing":
        admission = None
    elif tamper == "fabricated":
        admission = AdmittedSession(Principal("alice", "browser"), "session-1")
    elif tamper == "proof":
        admission = replace(admission, _proof=None)
    elif tamper == "session_id":
        admission = replace(admission, session_id="other")
    elif tamper == "principal":
        admission = replace(admission, principal=Principal("other", "browser"))
    elif tamper == "loaded_session":
        session.id = "other"
    elif tamper == "loaded_owner":
        session.owner = "other"
    elif tamper == "context_owner":
        kw["context_owner"] = "other"
    with pytest.raises(AdmissionDenied) as denied:
        await collect(
            GatewayTurns(TurnService()).stream_admitted_agent(
                admission, session, **kw,
            )
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"model": "foreign-model"},
    {"endpoint_url": "https://example.org"},
])
async def test_model_switch_requires_server_session_reconciliation(change):
    admission, session, kw = setup()
    kw.update(change)
    with pytest.raises(AdmissionDenied) as denied:
        await collect(
            GatewayTurns(TurnService()).stream_admitted_agent(
                admission, session, **kw,
            )
        )
    assert denied.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"authority": None},
    {"authority": object()},
    {"policy": None},
    {"policy": object()},
    {"bridge": object()},
    {"model": None},
    {"endpoint_url": ""},
    {"messages": ("hello",)},
    {"workspace": 42},
    {"disabled_tools": "web_search"},
    {"disabled_tools": [None]},
])
async def test_invalid_internal_turn_arguments_fail_closed(change):
    admission, session, kw = setup()
    kw.update(change)
    with pytest.raises(AdmissionDenied):
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "session_id", "history_session", "owner", "headers",
    "request_authority", "tool_policy", "delegated_credential",
])
async def test_caller_cannot_override_reserved_runtime_security_fields(field):
    admission, session, kw = setup()
    kw[field] = "spoof"
    with pytest.raises(AdmissionDenied):
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,session_id,workspace", [
    ("other", "session-1", None),
    ("alice", "foreign", None),
    ("alice", "session-1", "/workspace/other"),
])
async def test_foreign_authority_or_workspace_never_enters_agent_loop(
    owner, session_id, workspace,
):
    admission, session, kw = setup()
    kw["authority"] = RequestAuthority.empty(
        owner=owner, session_id=session_id, workspace=workspace,
    )
    with pytest.raises(AdmissionDenied) as denied:
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_runtime_denials_are_at_least_as_strict_as_server_policy(
    monkeypatch,
):
    admission, session, kw = setup(disabled={"web_search", "bash"})
    seen = {}

    async def fake_loop(*_args, **options):
        seen.update(options)
        yield "done"

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_loop)
    kw["disabled_tools"] = {"read_file"}
    assert await collect(GatewayTurns(TurnService()).stream_admitted_agent(
        admission, session, **kw,
    )) == ["done"]
    assert {"read_file", "web_search", "bash"} <= set(seen["disabled_tools"])
    assert {"web_search", "bash"} <= seen["request_authority"].denied


@pytest.mark.asyncio
async def test_selected_workspace_binding_is_exact(monkeypatch):
    admission, session, kw = setup(workspace="/workspace/test")
    received = {}

    async def fake_loop(*_args, **options):
        received.update(options)
        yield "done"

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_loop)
    assert await collect(GatewayTurns(TurnService()).stream_admitted_agent(
        admission, session, **kw,
    )) == ["done"]
    assert received["workspace"] == "/workspace/test"
    assert received["request_authority"].workspace == "/workspace/test"




@pytest.mark.asyncio
async def test_gateway_composes_admission_authority_execution_and_direct_delivery(
    monkeypatch,
):
    """A synthetic full turn runs through Gateway without a Web/HTTP adapter."""
    admission, session, kw = setup(disabled={"bash"})
    gateway = GatewayTurns(TurnService())
    kw["authority"] = gateway.build_authority(
        admission,
        "Hello",
        workspace=None,
        history=[],
        policy=kw["policy"],
        internal_tool_request=False,
    )
    observed = {}

    async def fake_provider(*_args, **options):
        observed.update(options)
        yield 'data: {"type":"delta","delta":"Hi"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_provider)
    delivery = gateway.open_stream(
        admission,
        gateway.stream_admitted_agent(admission, session, **kw),
        compare_mode=True,
        client_runtime_context={
            "surface": "odysseus-native",
            "unattended_mode": True,
        },
    )
    assert not delivery.detached
    assert delivery.run_id is None
    assert await collect(delivery.events) == [
        'data: {"type":"delta","delta":"Hi"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert observed["request_authority"].bound_to(
        owner="alice", session_id="session-1",
    )
    assert "bash" in observed["disabled_tools"]
    assert observed["history_session"] is session




@pytest.mark.asyncio
async def test_native_terminal_opt_in_is_denied_without_strict_tool_policy():
    admission, session, kw = setup()
    kw["native_terminal_events"] = True
    with pytest.raises(AdmissionDenied) as denied:
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_native_terminal_rejects_nonboolean_setting():
    admission, session, kw = setup(disabled={"bash"})
    kw["native_terminal_events"] = "true"
    with pytest.raises(AdmissionDenied) as denied:
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_native_terminal_rejects_execution_bridge_even_when_tools_disabled():
    from dataclasses import replace

    async def unused_tool(*_a, **_kw):
        raise AssertionError("unexpected tool")

    admission, session, kw = setup()
    kw["policy"] = replace(
        kw["policy"], block_all_tool_calls=True, disable_mcp=True,
    )
    kw["bridge"] = AgentExecutionBridge(
        unused_tool, frozenset({"web_search"}), name="deny-native-bridge",
    )
    kw["native_terminal_events"] = True
    with pytest.raises(AdmissionDenied) as denied:
        await collect(GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        ))
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_strict_admitted_agent_can_yield_native_core_terminal(monkeypatch):
    from dataclasses import replace
    from src.agent_core_events import CoreTerminalEvent, completed_core_event

    admission, session, kw = setup()
    kw["policy"] = replace(
        kw["policy"], block_all_tool_calls=True, disable_mcp=True,
    )
    kw["native_terminal_events"] = True
    calls = []

    async def fake_core(*_a, **options):
        calls.append(options)
        yield 'data: {"delta":"Answer"}\n\n'
        yield completed_core_event()

    monkeypatch.setattr(turn_service_module, "stream_agent_loop", fake_core)
    data = await collect(
        GatewayTurns(TurnService()).stream_admitted_agent(
            admission, session, **kw,
        )
    )
    assert data[0] == 'data: {"delta":"Answer"}\n\n'
    assert isinstance(data[1], CoreTerminalEvent)
    assert calls[0]["native_terminal_events"] is True
    assert calls[0]["tool_policy"].block_all_tool_calls
    assert calls[0]["tool_policy"].disable_mcp


def test_execution_service_is_transport_independent_and_does_not_duplicate_loop():
    from src import gateway_agent_execution
    source = inspect.getsource(gateway_agent_execution)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("routes", "fastapi", "starlette"),
            )
        elif isinstance(node, ast.Import):
            assert all(
                not imported.name.startswith(("routes", "fastapi", "starlette"))
                for imported in node.names
            )
    assert "stream_agent_loop(" not in source
    assert "turns.stream_agent(" in source
    assert not hasattr(gateway_agent_execution, "router")


def test_web_handoff_uses_gateway_execution_after_bound_authority():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text(encoding="utf-8")
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.count("gateway_turns.stream_admitted_agent(") == 1
    assert stream.index("gateway_turns.build_authority(") < stream.index(
        "gateway_turns.stream_admitted_agent("
    )
    assert stream.index("gateway_turns.scope_routed_contract(") < stream.index(
        "gateway_turns.stream_admitted_agent("
    )
    assert "turn_service.stream_agent(" not in stream
    assert "authority=_request_authority" in stream
    assert "policy=tool_policy" in stream
    assert "context_owner=ctx.user" in stream
