"""M5.2 integration: canonical registry governs the shared execution flow.

Covers the inbound boundary on the native tool-call channel: canonical calls
are validated and translated exactly once before every other guard, rejected
calls never execute and still receive truthful feedback, the resolution audit
understands canonical names, and the dispatcher refuses rejected records.
"""
import json

import pytest

import src.agent_loop as al
from src.tool_execution import execute_tool_block
from src.tool_registry import (
    REJECTED_CALL_TOOL_TYPE,
    ToolRegistryError,
    rejected_block_result,
    resolve_model_call,
)
from src.tool_types import ToolBlock

OFFERED = {
    "read_file", "write_file", "edit_file", "glob", "grep",
    "bash", "web_fetch", "web_search", "manage_notes", "python",
}


def _call(name, arguments="{}"):
    return {"id": f"call_{name}", "type": "function", "name": name, "arguments": arguments}


def _resolve(native_calls, **overrides):
    kwargs = {
        "is_api_model": True,
        "offered_tool_names": set(OFFERED),
    }
    kwargs.update(overrides)
    return al._resolve_tool_blocks("", native_calls, 1, **kwargs)


def test_canonical_native_call_converts_to_handler_identity():
    tool_blocks, used_native, converted = _resolve([
        _call("Read", json.dumps({"file_path": "a.py", "offset": 2, "limit": 5})),
    ])
    assert used_native is True
    assert [(b.tool_type, json.loads(b.content)) for b in tool_blocks] == [
        ("read_file", {"path": "a.py", "offset": 2, "limit": 5}),
    ]
    # The converted call carries the internal handler identity, so offered
    # checks, approval fingerprints and history all stay handler-keyed.
    assert converted[0]["name"] == "read_file"
    assert json.loads(converted[0]["arguments"]) == {"path": "a.py", "offset": 2, "limit": 5}


def test_canonical_native_call_is_not_dropped_as_unoffered():
    # Before the registry gate, a canonical name could not match the
    # handler-id offered set and was silently dropped for API models.
    tool_blocks, used_native, converted = _resolve([_call("Bash", json.dumps({"command": "ls"}))])
    assert used_native is True
    assert tool_blocks[0].tool_type == "bash"
    assert converted[0]["name"] == "bash"


@pytest.mark.parametrize("legacy_name,args", [
    ("read_file", json.dumps({"path": "a.py"})),
    ("shell", json.dumps({"command": "ls"})),
    ("websearch", json.dumps({"query": "x"})),
])
def test_legacy_native_calls_are_rejected_without_execution(legacy_name, args):
    tool_blocks, used_native, converted = _resolve([_call(legacy_name, args)])
    assert used_native is True
    assert tool_blocks[0].tool_type == REJECTED_CALL_TOOL_TYPE
    record = rejected_block_result(tool_blocks[0].content)
    assert record["blocked"] is True
    assert record["execution_attempted"] is False
    assert record["rejection_code"] == "legacy_tool_name"
    assert "Read" in record["error"] or "Bash" in record["error"] or "WebSearch" in record["error"]
    # The model's own call is echoed for the continuation history.
    assert converted[0]["name"] == legacy_name


def test_invalid_canonical_arguments_are_rejected_with_guidance():
    tool_blocks, _used, _converted = _resolve([
        _call("Grep", json.dumps({"pattern": "x", "output_mode": "content"})),
    ])
    assert tool_blocks[0].tool_type == REJECTED_CALL_TOOL_TYPE
    record = rejected_block_result(tool_blocks[0].content)
    assert record["rejection_code"] == "invalid_tool_arguments"
    assert "output_mode" in record["error"]


def test_skill_activates_through_the_registry_and_dispatch():
    # M5.2 increment 5: Skill is a real registry tool. Its call must resolve
    # to the activate_skill handler and dispatch through the real handler
    # under server request authority; with no skill library bound to this
    # owner the result is a truthful not-found — never a simulated activation.
    import asyncio

    from src.agent_runtime.authority import OperationGrant, RequestAuthority
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT

    tool_blocks, _used, _converted = _resolve(
        [_call("Skill", json.dumps({"name": "anything"}))],
        offered_tool_names=set(OFFERED) | {"activate_skill"},
    )
    assert tool_blocks[0].tool_type == "activate_skill"
    authority = RequestAuthority("req", "alice", "turn-session", "", (OperationGrant("activate_skill"),))
    _desc, result = asyncio.run(execute_tool_block(
        tool_blocks[0],
        session_id="turn-session", owner="alice",
        request_authority=authority,
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert result["exit_code"] == 1
    assert "not installed" in result["error"]
    assert "Call Skill without arguments" in result["error"]


def test_each_rejected_call_receives_its_own_feedback():
    tool_blocks, _used, converted = _resolve([
        _call("read_file", "{}"),
        _call("read_file", "{}"),
        _call("Read", json.dumps({"file_path": "a.py"})),
    ])
    rejected = [b for b in tool_blocks if b.tool_type == REJECTED_CALL_TOOL_TYPE]
    assert len(rejected) == 2
    assert len(converted) == 3
    assert tool_blocks[-1].tool_type == "read_file"


def test_non_registry_native_calls_are_untouched():
    calls = [
        _call("manage_notes", json.dumps({"action": "list"})),
        _call("mcp__github__list_issues", "{}"),
        _call("python", json.dumps({"code": "print(1)"})),
    ]
    offered = set(OFFERED) | {"mcp__github__list_issues"}
    tool_blocks, used_native, converted = _resolve(calls, offered_tool_names=offered)
    assert [b.tool_type for b in tool_blocks] == ["manage_notes", "mcp__github__list_issues", "python"]
    assert [c["name"] for c in converted] == ["manage_notes", "mcp__github__list_issues", "python"]


def test_offered_check_still_drops_unknown_native_calls():
    tool_blocks, _used, _converted = _resolve([
        _call("host_shell", json.dumps({"command": "ls"})),
    ], offered_tool_names={"read_file", "bash"})
    assert all(b.tool_type != "host_shell" for b in tool_blocks)


def test_resolution_audit_understands_canonical_names():
    # A canonical call accepted under its handler id is not a rejected request.
    assert al._native_tool_name_was_accepted("Read", {"read_file"}) is True
    assert al._native_tool_name_was_accepted("Bash", {"bash", REJECTED_CALL_TOOL_TYPE}) is True
    # A genuinely rejected legacy call is not reported as accepted.
    assert al._native_tool_name_was_accepted("read_file", {REJECTED_CALL_TOOL_TYPE}) is False
    # Non-registry matching keeps its MCP leaf behavior.
    assert al._native_tool_name_was_accepted("mcp__email__list_emails", {"mcp__email__list_emails"}) is True


def test_execute_tool_block_refuses_rejected_records():
    import asyncio

    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT

    content = json.dumps({
        "requested_tool": "Read",
        "requested_arguments": "{}",
        "result": {
            "error": "'read_file' is not a tool name; use Read.",
            "exit_code": 1, "blocked": True, "execution_attempted": False,
            "failure_kind": "tool_registry_rejected", "rejection_code": "legacy_tool_name",
            "rejected_tool": "read_file",
        },
    }, ensure_ascii=False)
    desc, result = asyncio.run(execute_tool_block(
        ToolBlock(REJECTED_CALL_TOOL_TYPE, content),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert "rejected" in desc
    assert result["blocked"] is True
    assert result["execution_attempted"] is False
    assert result["failure_kind"] == "tool_registry_rejected"


def test_converter_resolves_canonical_names_for_textual_paths():
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block("Read", json.dumps({"file_path": "a.py"}))
    assert block.tool_type == "read_file"
    # The internal handler encoding (line form for a single-arg read) is
    # identical to a legacy native call's.
    assert block.content == "a.py"

    rejected = function_call_to_tool_block("Read", json.dumps({"file_path": "a.py", "bogus": 1}))
    assert rejected.tool_type == REJECTED_CALL_TOOL_TYPE

    # Legacy spellings keep their inherited converter behavior this increment.
    legacy = function_call_to_tool_block("web_search", json.dumps({"query": "x"}))
    assert legacy.tool_type == "web_search"


def test_xml_invoke_keeps_exact_case_for_canonical_names():
    from src.tool_parsing import parse_tool_blocks

    blocks = parse_tool_blocks(
        '<invoke name="Read"><parameter name="file_path">a.py</parameter></invoke>',
        skip_fenced=True,
    )
    assert len(blocks) == 1
    assert blocks[0].tool_type == "read_file"
    assert blocks[0].content == "a.py"


def test_end_to_end_canonical_call_executes_real_handler_and_continues(monkeypatch, tmp_path):
    """Full flow evidence: canonical declaration → wire projection → native
    call validation → real dispatch → result → agent continuation, with the
    REAL executor and filesystem (only the LLM stream is synthetic)."""
    import asyncio
    import src.tool_execution as execution
    from src.tool_capabilities import ToolGateDecision

    target = tmp_path / "hello.txt"
    target.write_text("hello registry", encoding="utf-8")

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    # Filesystem tools are admin-restricted on public deployments; this
    # fixture simulates the authorized admin path while keeping the REAL
    # dispatcher, resource bindings and filesystem handler.
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: owner == "admin")
    monkeypatch.setattr(
        al.ToolRunSecurityContext,
        "decision_for",
        lambda self, *a, **k: ToolGateDecision(True),
        raising=False,
    )

    async def _fake_stream(_candidates, messages, **kwargs):
        if not any(
            isinstance(m, dict) and m.get("role") == "tool" for m in messages
        ):
            # Round 1: the loop offers registry tools under handler ids; the
            # wire projection to canonical names happens inside the transport
            # (verified separately) — here the fake stands in for the provider.
            offered = [t["function"]["name"] for t in (kwargs.get("tools") or [])]
            assert "read_file" in offered, offered
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{
                    "id": "call-read-1",
                    "name": "Read",
                    "arguments": json.dumps({"file_path": str(target)}),
                }],
            }) + "\n\n"
        else:
            # Round 2: the model sees the real tool result and answers from it.
            tool_msg = next(m for m in messages if m.get("role") == "tool")
            yield "data: " + json.dumps({"delta": f"Answer: {tool_msg['content']}"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    chunks = []

    async def _run():
        async for chunk in al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [{"role": "user", "content": f"Read {target} and tell me what it says."}],
            max_rounds=3,
            relevant_tools={"read_file"},
            workspace=str(tmp_path),
            owner="admin",
        ):
            chunks.append(chunk)

    asyncio.run(_run())
    text = "".join(chunks)
    assert "hello registry" in text, text
    # The tool result reached the provider history with the canonical call name.
    assert '"tool_call_id": "call-read-1"' in text or '"call_id": "call-read-1"' in text


def test_model_request_snapshot_mirrors_the_canonical_wire():
    snapshot = al._model_request_snapshot(
        round_num=1, model="claude-opus-5",
        messages=[{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
        }],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        temperature=0.0, max_tokens=100, prompt_type=None,
    )
    assert snapshot["tools"][0]["function"]["name"] == "Read"
    assert snapshot["messages"][0]["tool_calls"][0]["function"]["name"] == "Read"
    assert json.loads(snapshot["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {
        "file_path": "a.py",
    }
