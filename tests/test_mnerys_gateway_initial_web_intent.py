"""M2.1 pure pre-admission URL/browser intent parity and isolation tests.

Independent reference reproduces the old Web adapter's classification order;
the lexical helpers themselves remain covered by existing route compatibility
tests. No session, DB, browser, tool call or network access is involved.
"""

import ast
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.gateway_initial_web_intent import (
    InitialWebIntent, classify_initial_web_intent,
)
from src.gateway_turns import GatewayTurns
from src.turn_contract import requires_external_web_verification
from routes.chat_routes import (
    _authorizes_exact_url_fetch, _contains_explicit_url_target,
    _is_explicit_browser_automation_request, _is_external_discovery_request,
    _is_personal_data_search_without_web_target,
)


def original_route_reference(message, search_enabled, workspace_bound, renderer):
    """Literal baseline of the previous route's decision precedence."""
    web = False
    personal = False
    target = False
    exact = False
    browser = False
    discovery = False
    private = False
    local_render = False
    if isinstance(message, str):
        text = message.lower()
        explicit_url = _contains_explicit_url_target(text)
        personal = _is_personal_data_search_without_web_target(text)
        target = bool(re.search(
            r"\b(?:web|internet|online|google|news|weather|website|url|browse|browser)\b",
            text,
        )) or explicit_url
        web = (
            explicit_url
            or bool(re.search(
                r"\b(search|look\s+(?:(?:this|that|it|them|these|those)\s*)?up|lookup|find\s*out|google|browse|web|online|latest|current|today|news|weather|forecast|rate|exchange\s+rate)\b",
                text,
            ))
            or requires_external_web_verification(message)
        ) and (not personal or target)
        browser = _is_explicit_browser_automation_request(text)
        discovery = _is_external_discovery_request(text)
        if discovery:
            web = True
        exact = _authorizes_exact_url_fetch(text)
        private = bool(re.search(
            r"\bprivate[_ -]?brow(?:ser|esr|sr)\b", text,
        )) or bool(re.search(
            r"\bagent\s+unsubscribe\b.*\bhttps?://", text, re.DOTALL,
        ))
        if private:
            browser = True
            web = False
        local_render = bool(
            workspace_bound and renderer()
        )
    allow_browser = bool(
        browser or local_render or (web and not personal) or search_enabled
    )
    return (web, personal, target, exact, browser, discovery, private,
            local_render, allow_browser)


@pytest.mark.parametrize("case", [
    {"message": None},
    {"message": 7},
    {"message": ""},
    {"message": "Hello"},
    {"message": "Find my notes"},
    {"message": "Search my previous chats for the old parser"},
    {"message": "Search my old meetings"},
    {"message": "What events do I have today?"},
    {"message": "Search latest headlines on the internet"},
    {"message": "Find my notes and check the latest news online"},
    {"message": "Search memory for yesterday's meeting"},
    {"message": "Read https://example.com/report"},
    {"message": "Summarize https://example.com/report.pdf"},
    {"message": "Summarize https://youtu.be/demo"},
    {"message": "Do not search or fetch https://example.com/private"},
    {"message": "Open https://example.com and click the contact link"},
    {"message": "Go to example.com and fill the form"},
    {"message": "Navigate to the site and submit a form"},
    {"message": "Open with private_browesr https://example.com"},
    {"message": "Use private_browser to submit my response"},
    {"message": "agent unsubscribe for me https://example.com/leave"},
    {"message": "Find the official announcement"},
    {"message": "Locate the press release"},
    {"message": "Find my announcement note"},
    {"message": "Open /workspace/output.html"},
    {"message": "Save /workspace/fixtures/video.mp4"},
    {"message": "Inspect https://arxiv.org/pdf/2311.08526"},
    {"message": "Hello", "search_enabled": True},
    {"message": None, "search_enabled": True},
    {"message": "Hello", "workspace_bound": True, "render_required": True},
    {"message": "Hello", "workspace_bound": True, "render_required": False},
    {"message": "Hello", "workspace_bound": False, "render_required": True},
    {"message": "Create /workspace/page.html", "workspace_bound": True, "render_required": True},
    {"message": "Search my notes", "workspace_bound": True, "render_required": True},
    {"message": "Go to website", "workspace_bound": True, "render_required": True},
    {"message": None, "workspace_bound": True, "render_required": True},
    {"message": "Search my notes", "search_enabled": True, "workspace_bound": True, "render_required": False},
])
def test_initial_web_intent_preserves_inherited_routing_order(case):
    message = case.get("message")
    enabled = case.get("search_enabled", False)
    workspace = case.get("workspace_bound", False)
    render = case.get("render_required", False)
    old_lookup = MagicMock(return_value=render)
    lookup = MagicMock(return_value=render)
    expected = original_route_reference(message, enabled, workspace, old_lookup)
    result = classify_initial_web_intent(
        message=message, search_enabled=enabled,
        workspace_bound=workspace, local_browser_render_lookup=lookup,
    )
    assert isinstance(result, InitialWebIntent)
    assert (
        result.explicit_web_intent,
        result.personal_store_intent,
        result.explicit_web_target,
        result.exact_url_fetch_intent,
        result.explicit_browser_intent,
        result.external_discovery_intent,
        result.explicit_private_browser_intent,
        result.local_browser_render_intent,
        result.allow_browser_for_web_turn,
    ) == expected
    assert lookup.call_count == old_lookup.call_count
    assert not hasattr(result, "tool_grants")
    assert not hasattr(result, "admission")
    assert not hasattr(result, "session_id")


def test_gateway_facade_preserves_pure_preflight_output():
    options = dict(
        message="Summarize https://example.com/report",
        search_enabled=False,
        workspace_bound=False,
        local_browser_render_lookup=MagicMock(return_value=True),
    )
    direct = classify_initial_web_intent(**options)
    facade = GatewayTurns(MagicMock()).preview_initial_web_intent(**options)
    assert direct == facade
    options["local_browser_render_lookup"].assert_not_called()


def test_url_is_fetch_only_and_interactive_browser_is_not_search_permission():
    def resolve(text):
        return classify_initial_web_intent(
            message=text, search_enabled=False, workspace_bound=False,
            local_browser_render_lookup=lambda: False,
        )
    plain_url = resolve("Summarize https://example.com/report")
    assert plain_url.exact_url_fetch_intent
    assert not plain_url.explicit_browser_intent
    browser = resolve("Open https://example.com and click the link")
    assert browser.explicit_browser_intent
    assert not browser.exact_url_fetch_intent
    private = resolve("Use private_browser to open https://example.com")
    assert private.explicit_private_browser_intent
    assert private.explicit_browser_intent
    assert not private.explicit_web_intent
    youtube = resolve("Summarize https://youtu.be/example")
    assert not youtube.exact_url_fetch_intent
    forbidden = resolve("Do not search or fetch https://example.com/private")
    assert not forbidden.exact_url_fetch_intent


def test_local_html_render_requires_workspace_without_enabling_open_search():
    lookup = MagicMock(return_value=True)
    result = classify_initial_web_intent(
        message="Create /workspace/report.html", search_enabled=False,
        workspace_bound=True, local_browser_render_lookup=lookup,
    )
    lookup.assert_called_once()
    assert result.local_browser_render_intent
    assert result.allow_browser_for_web_turn
    assert not result.exact_url_fetch_intent
    assert not result.explicit_browser_intent


@pytest.mark.parametrize("change", [
    {"search_enabled": "true"},
    {"search_enabled": 1},
    {"workspace_bound": "yes"},
    {"workspace_bound": None},
    {"local_browser_render_lookup": True},
    {"local_browser_render_lookup": None},
])
def test_invalid_server_inputs_rejected_before_local_browser_check(change):
    lookup = MagicMock(return_value=True)
    options = dict(
        message="Create /workspace/report.html", search_enabled=False,
        workspace_bound=True, local_browser_render_lookup=lookup,
    )
    options.update(change)
    with pytest.raises(TypeError):
        classify_initial_web_intent(**options)
    lookup.assert_not_called()


def test_invalid_server_browser_render_evidence_is_rejected():
    lookup = MagicMock(return_value="yes")
    with pytest.raises(TypeError):
        classify_initial_web_intent(
            message="Create /workspace/report.html", search_enabled=False,
            workspace_bound=True, local_browser_render_lookup=lookup,
        )
    lookup.assert_called_once()


def test_helper_aliases_are_same_functions_not_duplicate_classifiers():
    import routes.chat_routes as route
    import src.gateway_initial_web_intent as service
    for helper in (
        "_is_personal_data_search_without_web_target",
        "_explicitly_denies_web_lookup",
        "_contains_explicit_url_target",
        "_authorizes_exact_url_fetch",
        "_is_explicit_browser_automation_request",
        "_is_external_discovery_request",
    ):
        assert getattr(route, helper) is getattr(service, helper)


def test_service_has_no_web_transport_persistence_or_executor_imports():
    import src.gateway_initial_web_intent as service
    tree = ast.parse(inspect.getsource(service))
    banned = ("routes", "fastapi", "starlette", "core.database",
              "src.gateway_admission", "src.tool_execution", "src.agent_loop")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(banned)
        elif isinstance(node, ast.Import):
            assert all(not imported.name.startswith(banned) for imported in node.names)


def test_web_handoff_occurs_before_agent_preflight_and_owner_admission():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text()
    stream = source[source.index('@router.post("/api/chat_stream")'):]
    # The stream handler is nested inside setup_chat_routes, so its
    # decorator is not valid as a standalone top-level Python fragment.
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "preview_initial_web_intent"
    ]
    assert len(calls) == 1
    args = {kw.arg: kw.value for kw in calls[0].keywords}
    assert isinstance(args["workspace_bound"], ast.Call)
    assert isinstance(args["local_browser_render_lookup"], ast.Lambda)
    assert stream.index("gateway_turns.preview_initial_web_intent(") < stream.index(
        "gateway_turns.preview_initial_agent_route("
    ) < stream.index("_admitted_session = _verify_session_owner(")
    assert stream.index("gateway_turns.preview_initial_web_intent(") < stream.index(
        "gateway_turns.finalize_tool_policy("
    )
    assert "requires_external_web_verification(message)" not in stream
