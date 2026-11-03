"""M2.1: post-response side effects require the issued Gateway admission."""
import ast
import inspect
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission,
    Principal, SessionOwnership,
)
from src.gateway_post_response import (
    dispatch_admitted_post_response, post_response_extraction_allowed,
)
from src.gateway_turns import GatewayTurns


def admitted(*, owner="alice", credential="browser", session_id="owned-chat"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session_id,
        lambda _sid: SessionOwnership(owner_id=owner),
    )


def execute(grant=None, sess=None, runner=None, **overrides):
    grant = admitted() if grant is None else grant
    sess = (
        SimpleNamespace(id=grant.session_id, owner=grant.principal.owner_id)
        if sess is None else sess
    )
    runner = runner or MagicMock()
    args = dict(
        context_owner=grant.principal.owner_id,
        task_runner=runner,
        message="request",
        full_response="answer",
        last_metrics={"input_tokens": 7},
        preferences={"auto_memory": True},
        memory_manager=object(),
        memory_vector=object(),
        webhook_manager=object(),
        tools_blocked=False,
    )
    args.update(overrides)
    dispatch_admitted_post_response(grant, sess, object(), **args)
    return runner


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
def test_admitted_owner_is_forwarded_to_inherited_runner(credential):
    grant = admitted(owner="alice", credential=credential)
    sess = SimpleNamespace(id=grant.session_id, owner=grant.principal.owner_id)
    runner = MagicMock()
    execute(
        grant, sess, runner,
        context_owner=grant.principal.owner_id,
        extract_skills=False,
        agent_rounds=4,
        agent_tool_calls=5,
    )
    runner.assert_called_once()
    args, kw = runner.call_args
    assert args[0] is sess
    assert args[2] == grant.session_id
    assert args[3:6] == ("request", "answer", {"input_tokens": 7})
    assert kw["owner"] == grant.principal.owner_id
    assert kw["allow_background_extraction"] is True
    assert kw["extract_skills"] is False
    assert kw["agent_rounds"] == 4
    assert kw["agent_tool_calls"] == 5


@pytest.mark.parametrize(
    "tools_blocked,continuation,runtime_context,expected",
    [
        (False, False, None, True),
        (False, False, {}, True),
        (True, False, None, False),
        (False, True, None, False),
        (False, False, {"unattended_mode": True}, False),
        (False, False, {"surface": "odysseus-native", "unattended_mode": True}, False),
        (False, False, {"surface": "odysseus-tui", "unattended_mode": True}, False),
        (False, False, {"unattended_mode": "true"}, True),
        (False, False, "forged", True),
    ],
)
def test_background_extraction_policy_preserves_legacy_routing(
    tools_blocked, continuation, runtime_context, expected,
):
    assert post_response_extraction_allowed(
        tools_blocked=tools_blocked,
        tool_approval_continuation=continuation,
        client_runtime_context=runtime_context,
    ) is expected
    runner = execute(
        tools_blocked=tools_blocked,
        tool_approval_continuation=continuation,
        client_runtime_context=runtime_context,
    )
    assert runner.call_args.kwargs["allow_background_extraction"] is expected
    # Denying auxiliary LLM work must not prevent the normal post-response
    # runner from recording metrics, webhooks and auto-naming.
    runner.assert_called_once()


@pytest.mark.parametrize("forged", [
    None,
    "owned-chat",
    AdmittedSession(Principal("alice", "browser"), "owned-chat"),
])
def test_no_side_effects_without_server_issued_admission(forged):
    runner = MagicMock()
    with pytest.raises(AdmissionDenied) as raised:
        dispatch_admitted_post_response(
            forged, SimpleNamespace(id="owned-chat", owner="alice"), object(),
            context_owner="alice",
            task_runner=runner,
            message="request", full_response="answer", last_metrics=None,
            preferences={}, memory_manager=None, memory_vector=None,
            webhook_manager=None, tools_blocked=False,
        )
    assert raised.value.status_code == 403
    runner.assert_not_called()


@pytest.mark.parametrize("tamper", [
    "session",
    "principal",
    "proof",
    "loaded-owner",
    "context-owner",
])
def test_mismatched_owner_or_session_fails_before_side_effect(tamper):
    grant = admitted()
    sess = SimpleNamespace(id="owned-chat", owner="alice")
    owner = "alice"
    if tamper == "session":
        grant = replace(grant, session_id="another-chat")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "loaded-owner":
        sess.owner = "bob"
    elif tamper == "context-owner":
        owner = "bob"
    runner = MagicMock()
    with pytest.raises(AdmissionDenied):
        execute(grant, sess, runner, context_owner=owner)
    runner.assert_not_called()


@pytest.mark.parametrize("bad_flags", [
    {"tools_blocked": "false"},
    {"tool_approval_continuation": "false"},
    {"incognito": 0},
    {"compare_mode": "true"},
    {"extract_skills": 1},
])
def test_invalid_trusted_flags_deny_before_post_response(bad_flags):
    runner = MagicMock()
    with pytest.raises(AdmissionDenied):
        execute(runner=runner, **bad_flags)
    runner.assert_not_called()


def test_streaming_incognito_flags_reach_original_runner_unchanged():
    runner = execute(
        incognito=True, compare_mode=True, character_name="Mascot",
        skills_manager=object(), extract_skills=False,
    )
    kw = runner.call_args.kwargs
    assert kw["incognito"] is True
    assert kw["compare_mode"] is True
    assert kw["character_name"] == "Mascot"
    assert kw["extract_skills"] is False


def test_gateway_facade_uses_the_same_admitted_post_response_service():
    grant = admitted()
    sess = SimpleNamespace(id=grant.session_id, owner="alice")
    runner = MagicMock()
    facade = GatewayTurns(MagicMock())
    facade.dispatch_post_response(
        grant, sess, object(),
        context_owner="alice", task_runner=runner,
        message="hello", full_response="world", last_metrics=None,
        preferences={}, memory_manager=None, memory_vector=None,
        webhook_manager=None, tools_blocked=False,
    )
    runner.assert_called_once()
    assert runner.call_args.kwargs["owner"] == "alice"


def test_application_post_response_service_has_no_web_imports():
    from src import gateway_post_response

    tree = ast.parse(inspect.getsource(gateway_post_response))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi"))
        if isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith(("routes", "fastapi"))
                for alias in node.names
            )


def test_web_routes_delegate_all_three_post_response_paths():
    from pathlib import Path

    routes = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(routes)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "dispatch_post_response"
    ]
    assert len(calls) == 3
    for node in calls:
        kw = {item.arg: item.value for item in node.keywords}
        assert isinstance(kw["context_owner"], ast.Attribute)
        assert isinstance(kw["task_runner"], ast.Name)
        assert kw["task_runner"].id == "run_post_response_tasks"
        assert isinstance(kw["tools_blocked"], ast.Attribute)

    direct_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_post_response_tasks"
    ]
    assert not direct_calls
