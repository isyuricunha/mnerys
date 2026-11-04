"""M2.1 pure, unprivileged Chat-to-Agent preliminary routing parity tests."""

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.action_intents import ToolIntent, classify_tool_intent
from src.gateway_initial_agent_routing import InitialAgentRoute, select_initial_agent_route
from src.gateway_turns import GatewayTurns


def old_web_reference(*, message, chat_mode, allow_bash, search_enabled,
                      explicit_web_intent, explicit_private_browser_intent,
                      native_workspace_inputs):
    """Independent reproduction of the inherited pre-session Web precedence."""
    intent = classify_tool_intent(message) if isinstance(message, str) else None
    workspace_intent = bool(
        (intent and intent.needs_tools and intent.category in {"shell", "workspace"})
        or native_workspace_inputs
    )
    mode = chat_mode
    auto = False
    reason = None
    bash = allow_bash
    if chat_mode == "chat" and intent and intent.needs_tools:
        mode = "agent"
        auto = True
        reason = "tool_intent"
        if workspace_intent:
            bash = "true"
    elif chat_mode == "chat" and search_enabled:
        mode = "agent"
        auto = True
        reason = "search_enabled"
    elif chat_mode == "chat" and explicit_web_intent:
        mode = "agent"
        auto = True
        reason = "explicit_web_intent"
    elif chat_mode == "chat" and explicit_private_browser_intent:
        mode = "agent"
        auto = True
        reason = "explicit_private_browser_intent"
    return mode, intent, workspace_intent, auto, bash, reason


@pytest.mark.parametrize("case", [
    {},
    {"message": "Create a note about the meeting"},
    {"message": "How do I create a note?"},
    {"message": "Search the web for official documentation"},
    {"message": "Create a calendar event tomorrow"},
    {"message": "List files in my workspace"},
    {"message": "Please execute a shell command"},
    {"message": None},
    {"message": 123},
    {"message": "Hello", "search_enabled": True},
    {"message": "Hello", "explicit_web_intent": True},
    {"message": "Hello", "explicit_private_browser_intent": True},
    {"message": "Hello", "native_workspace_inputs": True},
    {"message": "Hello", "native_workspace_inputs": True, "search_enabled": True},
    {"message": "Create a note", "native_workspace_inputs": True},
    {"message": "Create a note", "search_enabled": True},
    {"message": "Create a note", "explicit_web_intent": True},
    {"message": "Hello", "search_enabled": True, "explicit_web_intent": True},
    {"message": "Hello", "explicit_web_intent": True, "explicit_private_browser_intent": True},
    {"message": "Hello", "chat_mode": "agent", "native_workspace_inputs": True},
    {"message": "Create a note", "chat_mode": "agent"},
    {"message": "Hello", "chat_mode": "chat", "allow_bash": "false"},
    {"message": "Hello", "chat_mode": "agent", "allow_bash": None},
    {"message": "", "chat_mode": "chat", "search_enabled": True},
    {"message": "Please execute a shell command", "allow_bash": "false"},
])
def test_pre_session_selection_matches_inherited_web_order(case):
    kwargs = dict(
        message=case.get("message", "Hello"),
        chat_mode=case.get("chat_mode", "chat"),
        allow_bash=case.get("allow_bash", None),
        search_enabled=case.get("search_enabled", False),
        explicit_web_intent=case.get("explicit_web_intent", False),
        explicit_private_browser_intent=case.get("explicit_private_browser_intent", False),
        native_workspace_inputs=case.get("native_workspace_inputs", False),
    )
    expected = old_web_reference(**kwargs)
    selected = select_initial_agent_route(**kwargs)
    assert isinstance(selected, InitialAgentRoute)
    assert (
        selected.chat_mode,
        selected.tool_intent,
        selected.workspace_agent_intent,
        selected.auto_escalated,
        selected.allow_bash,
        selected.escalation_reason,
    ) == expected
    assert GatewayTurns(MagicMock()).preview_initial_agent_route(**kwargs) == selected
    assert not hasattr(selected, "authority")
    assert not hasattr(selected, "grants")


@pytest.mark.parametrize(("category", "native", "expected"), [
    ("shell", False, "true"),
    ("workspace", False, "true"),
    ("calendar", False, "false"),
    ("notes", False, "false"),
    ("calendar", True, "true"),
])
def test_inherited_workspace_toggle_is_only_a_pre_admission_hint(
    monkeypatch, category, native, expected,
):
    from src import gateway_initial_agent_routing as module

    monkeypatch.setattr(
        module, "classify_tool_intent",
        lambda _: ToolIntent(True, category, "fixture action"),
    )
    selected = module.select_initial_agent_route(
        message="perform a task", chat_mode="chat", allow_bash="false",
        search_enabled=False, explicit_web_intent=False,
        explicit_private_browser_intent=False, native_workspace_inputs=native,
    )
    assert selected.chat_mode == "agent"
    assert selected.workspace_agent_intent == (category in {"shell", "workspace"} or native)
    assert selected.allow_bash == expected
    assert selected.escalation_reason == "tool_intent"
    # The routing result contains neither an issued admission nor a grant.
    assert not hasattr(selected, "session_id")
    assert not hasattr(selected, "tool_policy")


def test_existing_agent_mode_does_not_get_new_bash_permission(monkeypatch):
    from src import gateway_initial_agent_routing as module

    monkeypatch.setattr(
        module, "classify_tool_intent",
        lambda _: ToolIntent(True, "shell", "command"),
    )
    selected = module.select_initial_agent_route(
        message="run command", chat_mode="agent", allow_bash="false",
        search_enabled=True, explicit_web_intent=True,
        explicit_private_browser_intent=True, native_workspace_inputs=True,
    )
    assert selected.chat_mode == "agent"
    assert not selected.auto_escalated
    assert selected.allow_bash == "false"
    assert selected.workspace_agent_intent


def test_typed_action_precedes_search_and_browser_flags(monkeypatch):
    from src import gateway_initial_agent_routing as module

    monkeypatch.setattr(
        module, "classify_tool_intent",
        lambda _: ToolIntent(True, "notes", "create note"),
    )
    selected = module.select_initial_agent_route(
        message="create", chat_mode="chat", allow_bash=None,
        search_enabled=True, explicit_web_intent=True,
        explicit_private_browser_intent=True, native_workspace_inputs=False,
    )
    assert selected.escalation_reason == "tool_intent"
    assert selected.tool_intent.category == "notes"
    assert not selected.workspace_agent_intent


@pytest.mark.parametrize("overrides", [
    {"chat_mode": None},
    {"search_enabled": "true"},
    {"explicit_web_intent": 1},
    {"explicit_private_browser_intent": None},
    {"native_workspace_inputs": 1},
])
def test_invalid_internal_preflight_inputs_fail_closed_before_classification(
    monkeypatch, overrides,
):
    from src import gateway_initial_agent_routing as module

    classify = MagicMock(side_effect=AssertionError("classified an invalid request"))
    monkeypatch.setattr(module, "classify_tool_intent", classify)
    kwargs = dict(
        message="Create a note", chat_mode="chat", allow_bash=None,
        search_enabled=False, explicit_web_intent=False,
        explicit_private_browser_intent=False, native_workspace_inputs=False,
    )
    kwargs.update(overrides)
    with pytest.raises(TypeError):
        module.select_initial_agent_route(**kwargs)
    classify.assert_not_called()


def test_pure_preflight_has_no_http_persistence_or_execution_dependencies():
    from src import gateway_initial_agent_routing as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith((
                "routes", "fastapi", "starlette", "core.database",
                "src.tool_execution", "src.gateway_admission",
            ))
        if isinstance(node, ast.Import):
            assert all(
                not entry.name.startswith((
                    "routes", "fastapi", "starlette", "core.database",
                    "src.tool_execution", "src.gateway_admission",
                ))
                for entry in node.names
            )


def test_web_calls_pure_preflight_before_admitted_final_tool_policy():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    previews = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "preview_initial_agent_route"
    ]
    assert len(previews) == 1
    keywords = {k.arg: k.value for k in previews[0].keywords}
    assert isinstance(keywords["message"], ast.Name)
    assert keywords["message"].id == "message"
    assert isinstance(keywords["native_workspace_inputs"], ast.Call)
    # The module also has a separate /api/chat admission before this stream.
    # Compare within the stream route, not against that earlier endpoint.
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    assert stream.index("gateway_turns.preview_initial_agent_route(") < stream.index(
        "_admitted_session = _verify_session_owner("
    )
    assert source.index("gateway_turns.preview_initial_agent_route(") < source.index(
        "gateway_turns.finalize_tool_policy("
    )
    assert source.index("gateway_turns.preview_initial_agent_route(") < source.index(
        "gateway_turns.select_contract_scope("
    )
    assert "_classify_tool_intent(message)" not in source
