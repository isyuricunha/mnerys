"""Native catalog defaults and approval continuation regressions."""

from pathlib import Path

import pytest

from src.native_tool_catalog import (
    admitted_agent_tool_surface,
    is_local_tool_request,
    retry_local_task_after_irrelevant_tool_failures,
)


@pytest.mark.parametrize("utterance", [
    "experimente ls -la, cat e git status",
    "da um ls ai e me diz o que tem ai",
    "pwd",
    "cat README.md",
    "find . -name README.md",
    "git status",
])
def test_local_instructions_do_not_trigger_public_web_prefetch(utterance):
    assert is_local_tool_request(utterance)


@pytest.mark.parametrize("utterance", [
    "pesquise na internet sobre o comando ls",
    "search the web for git status examples",
    "open https://example.com and read the page",
    "qual a previsão do tempo?",
    "Why does my cat sleep so much?",
    "Find out what happened today",
    "Prices for cat food",
])
def test_web_requests_not_blindly_classified_local(utterance):
    assert not is_local_tool_request(utterance)


def test_approved_local_tool_and_other_catalog_capabilities_survive_resume():
    inventory = {
        "bash", "ls", "read_file", "web_fetch", "web_search",
        "tool_search", "get_workspace", "glob", "grep", "ask_user",
    }
    selected = admitted_agent_tool_surface(
        relevant=inventory,
        forced={"web_search", "web_fetch"},
        approval_selected={"bash", "ls", "read_file", "get_workspace"},
        approval_tool="bash",
        permitted=inventory,
        disabled=set(),
    )
    assert {"bash", "ls", "read_file", "get_workspace", "tool_search"} <= selected
    assert {"web_search", "web_fetch"} <= selected
    assert selected != {"web_search", "web_fetch"}


def test_policies_and_denials_override_warm_catalog_even_when_approved():
    selected = admitted_agent_tool_surface(
        relevant={"bash", "web_search", "write_file"},
        forced={"web_fetch"},
        approval_selected={"bash"},
        approval_tool="bash",
        permitted={"tool_search", "ls", "read_file", "web_search", "web_fetch"},
        disabled={"web_fetch", "web_search"},
    )
    assert selected == {"tool_search", "ls", "read_file"}
    assert "bash" not in selected


def test_one_bounded_local_recovery_after_unrelated_web_failures():
    events = [
        {"tool": "bash", "command": "ls -la", "exit_code": 0},
        {"tool": "web_fetch", "exit_code": 1, "blocked": True},
        {"tool": "web_fetch", "exit_code": 1, "blocked": True},
    ]
    params = dict(
        native_catalog=True, request_text="experimente ls -la, cat e git status",
        consecutive_failed_rounds=2, tool_events=events,
    )
    assert retry_local_task_after_irrelevant_tool_failures(
        **params, used_recovery=False,
    )
    assert not retry_local_task_after_irrelevant_tool_failures(
        **params, used_recovery=True,
    )
    assert not retry_local_task_after_irrelevant_tool_failures(
        **dict(params, native_catalog=False), used_recovery=False,
    )
    assert not retry_local_task_after_irrelevant_tool_failures(
        **dict(params, consecutive_failed_rounds=1), used_recovery=False,
    )
    assert not retry_local_task_after_irrelevant_tool_failures(
        **dict(params, request_text="search the web for docs"), used_recovery=False,
    )
    assert not retry_local_task_after_irrelevant_tool_failures(
        **dict(params, tool_events=[{"tool":"bash","exit_code":1}]),
        used_recovery=False,
    )


def test_full_inventory_is_not_eagerly_promoted_to_model_context():
    inventory = {"tool_search", "bash", "ls", "read_file", "web_fetch", "ask_user"}
    inventory.update({f"mcp__service__tool_{n}" for n in range(100)})
    visible = admitted_agent_tool_surface(
        relevant=(), forced=(), approval_selected=(), approval_tool=None,
        permitted=inventory, disabled=(),
    )
    assert "tool_search" in visible
    assert not any(item.startswith("mcp__") for item in visible)


def test_web_composer_gates_not_present_in_default_agent_markup():
    html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    assert 'id="web-toggle-btn"' not in html
    assert 'id="bash-toggle-btn"' not in html


def test_no_tool_feature_toggle_flags_in_agent_composer_post():
    js = (Path(__file__).resolve().parents[1] / "static/js/chat.js").read_text()
    assert "fd.append('allow_web_search'" not in js
    assert "fd.append('allow_bash'" not in js
