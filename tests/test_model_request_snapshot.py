from src.agent_loop import _model_request_capture_enabled, _model_request_snapshot
from src.tool_registry import project_tools_for_wire


def test_model_request_capture_is_opt_in(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_CAPTURE_MODEL_REQUESTS", raising=False)
    assert not _model_request_capture_enabled()
    monkeypatch.setenv("ODYSSEUS_CAPTURE_MODEL_REQUESTS", "true")
    assert _model_request_capture_enabled()


def test_model_request_snapshot_has_model_visible_fields_only():
    messages = [{"role": "system", "content": "route carefully"}]
    tools = [{"type": "function", "function": {"name": "manage_notes"}}]
    result = _model_request_snapshot(
        round_num=2,
        model="qwen",
        messages=messages,
        tools=tools,
        temperature=0.2,
        max_tokens=4096,
        prompt_type="agent",
        agent_prompt_mode="qwen_minimal",
    )
    assert result["messages"] is messages
    assert result["prompt_type"] is None
    assert "headers" not in result
    assert "endpoint" not in result


def test_model_request_snapshot_mirrors_the_provider_wire():
    """M5.2: the snapshot shows the canonical vocabulary the provider sees.

    Non-registry tools pass through unchanged; a registry-backed handler
    schema appears under its canonical Claude Code name exactly as llm_core
    projects it on the wire. The internal list object is never mutated.
    """
    tools = [
        {"type": "function", "function": {"name": "manage_notes"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]
    result = _model_request_snapshot(
        round_num=2,
        model="qwen",
        messages=[{"role": "system", "content": "route carefully"}],
        tools=tools,
        temperature=0.2,
        max_tokens=4096,
        prompt_type="agent",
        agent_prompt_mode="qwen_minimal",
    )
    assert [schema["function"]["name"] for schema in result["tools"]] == [
        "manage_notes", "Read",
    ]
    assert result["tools"] == project_tools_for_wire(tools)
    assert [schema["function"]["name"] for schema in tools] == [
        "manage_notes", "read_file",
    ]
