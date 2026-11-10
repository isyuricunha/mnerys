"""M5.2 increment 3: canonical tool vocabulary across textual execution paths.

The contract: a model-generated built-in call written in ANY textual format
(fenced block, XML invoke, Hermes/tool_call JSON, StepFun/tool_code/Gemma
tokens, raw OpenAI JSON leak) is admitted through the one canonical registry —
exact canonical names and argument contracts; legacy registry spellings are
rejected as inert records on governed turns and execute only on the documented
compatibility routes. Rejected blocks never dispatch.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.agent_tools  # noqa: F401,E402  (break the tool_parsing import cycle)

from src.tool_parsing import parse_tool_blocks, strip_tool_blocks  # noqa: E402
from src.tool_registry import (  # noqa: E402
    REJECTED_CALL_TOOL_TYPE,
    rejection_result,
    resolve_model_call,
)
from src.tool_schemas import (  # noqa: E402
    function_call_to_tool_block,
    textual_function_call_to_tool_block,
)
from src.tool_types import ToolBlock  # noqa: E402

GOVERNED = {"legacy_compat": False}


def _blocks(text, **kwargs):
    return parse_tool_blocks(text, **{**GOVERNED, **kwargs})


def _rejection(block):
    assert block.tool_type == REJECTED_CALL_TOOL_TYPE
    return json.loads(block.content)


# ---------------------------------------------------------------------------
# Canonical fenced calls
# ---------------------------------------------------------------------------

def test_canonical_read_fence_raw_path():
    blocks = _blocks("```Read\nnotes/todo.md\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("read_file", "notes/todo.md"),
    ]


def test_canonical_read_fence_json_with_range():
    blocks = _blocks('```Read\n{"file_path": "notes/todo.md", "offset": 3}\n```')
    assert len(blocks) == 1
    assert blocks[0].tool_type == "read_file"
    assert json.loads(blocks[0].content)["offset"] == 3


def test_canonical_write_fence_requires_json():
    blocks = _blocks(
        '```Write\n{"file_path": "notes/x.md", "content": "hello"}\n```')
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("write_file", "notes/x.md\nhello"),
    ]
    # A raw non-JSON body is rejected with actionable feedback, never executed.
    blocks = _blocks("```Write\nnotes/x.md\nhello\n```")
    envelope = _rejection(blocks[0])
    assert "Write" in envelope["result"]["error"]


def test_canonical_edit_fence_json():
    blocks = _blocks(
        '```Edit\n{"file_path": "a.md", "old_string": "a", "new_string": "b"}\n```')
    assert blocks[0].tool_type == "edit_file"
    assert json.loads(blocks[0].content) == {
        "path": "a.md", "old_string": "a", "new_string": "b",
    }


def test_canonical_bash_fence_and_inline_json():
    blocks = _blocks("```Bash\necho hi\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "echo hi")]
    blocks = _blocks('```Bash {"command": "echo inline"}\n```')
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "echo inline")]


def test_lowercase_bash_stays_a_language_fence_for_shell():
    blocks = _blocks("```bash\necho hi\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "echo hi")]
    # Empty content stays inert, exactly like the historical code fence.
    assert _blocks("```bash\n\n```") == []


def test_lowercase_python_stays_inherited_execution():
    blocks = _blocks("```python\nprint(1)\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [("python", "print(1)")]


def test_canonical_web_glob_grep_fences():
    blocks = _blocks("```WebSearch\nlatest rust release\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("web_search", "latest rust release"),
    ]
    blocks = _blocks('```WebSearch\n{"query": "rust", "time_filter": "day"}\n```')
    assert blocks[0].tool_type == "web_search"
    assert json.loads(blocks[0].content)["time_filter"] == "day"
    blocks = _blocks("```WebFetch\nhttps://example.com\n```")
    assert blocks[0].tool_type == "web_fetch"
    assert json.loads(blocks[0].content)["url"] == "https://example.com"
    blocks = _blocks("```Glob\n**/*.py\n```")
    assert json.loads(blocks[0].content) == {"pattern": "**/*.py"}
    blocks = _blocks('```Grep\n{"pattern": "def ", "path": "src"}\n```')
    assert blocks[0].tool_type == "grep"


def test_canonical_malformed_arguments_rejected_with_feedback():
    blocks = _blocks('```Edit\n{"file_path": "a.md"}\n```')
    envelope = _rejection(blocks[0])
    assert "Edit.old_string is required" in envelope["result"]["error"]

    blocks = _blocks("```Read\n\n```")
    envelope = _rejection(blocks[0])
    assert "Read" in envelope["result"]["error"]


def test_canonical_unsupported_argument_rejected():
    blocks = _blocks(
        '```WebSearch\n{"query": "rust", "allowed_domains": ["x"]}\n```')
    envelope = _rejection(blocks[0])
    assert "allowed_domains" in envelope["result"]["error"]


# ---------------------------------------------------------------------------
# Exact-case enforcement: legacy spellings and case variants
# ---------------------------------------------------------------------------

def test_governed_legacy_fence_names_rejected_inertly():
    for tag, body in (("read_file", "notes/todo.md"), ("web_search", "rust news"),
                      ("write_file", "a.md\nhi"), ("edit_file", '{"path": "a.md"}')):
        blocks = _blocks(f"```{tag}\n{body}\n```")
        envelope = _rejection(blocks[0])
        assert envelope["requested_tool"] == tag
        assert envelope["result"]["blocked"] is True
        assert envelope["result"]["execution_attempted"] is False


def test_governed_case_variants_rejected():
    for tag in ("READ_FILE", "Web_Search", "BASH", "Read_file"):
        blocks = _blocks(f"```{tag}\nx\n```")
        envelope = _rejection(blocks[0])
        assert envelope["result"]["blocked"] is True


def test_unavailable_tools_rejected_never_simulated():
    for tag in ("ToolSearch", "Skill"):
        blocks = _blocks(f"```{tag}\n{{}}\n```")
        envelope = _rejection(blocks[0])
        assert envelope["result"]["blocked"] is True


def test_compat_mode_keeps_legacy_fences_working():
    blocks = parse_tool_blocks("```read_file\nnotes/todo.md\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("read_file", "notes/todo.md"),
    ]
    blocks = parse_tool_blocks("```web_search\nrust news\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [("web_search", "rust news")]


def test_compat_mode_still_rejects_case_variants():
    blocks = parse_tool_blocks("```READ_FILE\nnotes/todo.md\n```")
    assert blocks[0].tool_type == REJECTED_CALL_TOOL_TYPE


def test_misfenced_legacy_rescue_rejected_on_governed_turns():
    blocks = _blocks('```bash\nread_file("notes/todo.md")\n```')
    assert blocks[0].tool_type == REJECTED_CALL_TOOL_TYPE
    # Compatibility keeps the historical rescue.
    blocks = parse_tool_blocks('```bash\nread_file("notes/todo.md")\n```')
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("read_file", "notes/todo.md"),
    ]


# ---------------------------------------------------------------------------
# XML invoke / direct tags
# ---------------------------------------------------------------------------

def test_xml_invoke_canonical_read():
    blocks = _blocks(
        'wrapper<invoke name="Read"><parameter name="file_path">a.md</parameter></invoke>')
    assert [(b.tool_type, b.content) for b in blocks] == [("read_file", "a.md")]


def test_xml_invoke_legacy_name_rejected_on_governed_turns():
    blocks = _blocks(
        'wrapper<invoke name="bash"><parameter name="command">ls</parameter></invoke>')
    assert blocks[0].tool_type == REJECTED_CALL_TOOL_TYPE


def test_xml_invoke_inherited_tool_passthrough():
    blocks = _blocks(
        '<invoke name="manage_notes"><parameter name="action">list</parameter></invoke>')
    assert blocks[0].tool_type == "manage_notes"


def test_xml_direct_canonical_tags():
    blocks = _blocks("<tool_call>\n<Bash>echo hi</Bash>")
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "echo hi")]
    blocks = _blocks("<tool_call>\n<WebFetch>https://example.com</WebFetch>")
    assert json.loads(blocks[0].content)["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# JSON/token text formats
# ---------------------------------------------------------------------------

def test_hermes_tool_call_body_canonical_and_legacy():
    canonical = _blocks(
        '<tool_call>\n{"name": "Bash", "arguments": {"command": "mkdir -p agent-test"}}\n')
    assert [(b.tool_type, b.content) for b in canonical] == [
        ("bash", "mkdir -p agent-test"),
    ]

    legacy = _blocks(
        '<tool_call>\n{"name": "bash", "arguments": {"command": "mkdir -p agent-test"}}\n')
    assert legacy[0].tool_type == REJECTED_CALL_TOOL_TYPE

    compat = parse_tool_blocks(
        '<tool_call>\n{"name": "bash", "arguments": {"command": "mkdir -p agent-test"}}\n')
    assert [(b.tool_type, b.content) for b in compat] == [
        ("bash", "mkdir -p agent-test"),
    ]


def test_stepfun_tokens_canonical():
    blocks = _blocks(
        '<｜tool▁call▁begin｜>Read<｜tool▁sep｜>{"file_path": "a.md"}<｜tool▁call▁end｜>')
    assert [(b.tool_type, b.content) for b in blocks] == [("read_file", "a.md")]


def test_tool_code_and_gemma_and_function_model_canonical():
    blocks = _blocks(
        "<tool_code>{tool => 'Bash', args => '<command>ls</command>'}</tool_code>")
    assert [(b.tool_type, b.content) for b in blocks] == [("bash", "ls")]

    blocks = _blocks('<|tool_call|>call:Read{"file_path": "a.md"}<|tool_call|>')
    assert [(b.tool_type, b.content) for b in blocks] == [("read_file", "a.md")]

    blocks = _blocks(
        "<function_model><function_call>Read</function_call>"
        "<parameters>{\"file_path\": \"a.md\"}</parameters></function_model>")
    assert [(b.tool_type, b.content) for b in blocks] == [("read_file", "a.md")]


def test_raw_openai_json_leak_admitted_through_registry():
    canonical = _blocks(
        '{"function": {"name": "Read", "arguments": "{\\"file_path\\": \\"a.md\\"}"},'
        ' "type": "function"}')
    assert [(b.tool_type, b.content) for b in canonical] == [("read_file", "a.md")]

    legacy = _blocks(
        '{"function": {"name": "web_search", "arguments": "{\\"query\\": \\"x\\"}"},'
        ' "type": "function"}')
    assert legacy[0].tool_type == REJECTED_CALL_TOOL_TYPE


def test_prose_web_json_recovery_follows_canonical_contract():
    text = 'Need to web_search for it.\n{"query": "latest rust release"}'
    blocks = _blocks(text)
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("web_search", "latest rust release"),
    ]


def test_inherited_and_mcp_tools_keep_their_identity():
    blocks = _blocks('```manage_notes\n{"action": "list"}\n```')
    assert [(b.tool_type, b.content) for b in blocks] == [
        ("manage_notes", '{"action": "list"}'),
    ]
    # Email tools keep their historical bare-name fence identity
    # (the dispatcher aliases bare <-> mcp__email__ forms for policy).
    blocks = _blocks("```list_emails\n{}\n```")
    assert [b.tool_type for b in blocks] == ["list_emails"]


# ---------------------------------------------------------------------------
# Native/textual parity and display-strip consistency
# ---------------------------------------------------------------------------

def test_native_and_textual_calls_produce_identical_blocks():
    for name, args in (
        ("Read", {"file_path": "a.md"}),
        ("Write", {"file_path": "a.md", "content": "hi"}),
        ("Edit", {"file_path": "a.md", "old_string": "a", "new_string": "b"}),
        ("Bash", {"command": "echo hi"}),
        ("WebSearch", {"query": "rust release"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Grep", {"pattern": "def "}),
    ):
        native = function_call_to_tool_block(name, json.dumps(args))
        resolved = resolve_model_call(name, json.dumps(args))
        textual = textual_function_call_to_tool_block(
            name, json.dumps(args), legacy_compat=False)
        assert native == textual, name
        assert resolved.governed and resolved.handler == native.tool_type, name


def test_strip_tool_blocks_removes_executed_canonical_fence():
    text = "Opening:\n```Read\nnotes/todo.md\n```\nDone."
    cleaned = strip_tool_blocks(text)
    assert "```" not in cleaned
    assert "notes/todo.md" not in cleaned
    assert "Opening:" in cleaned and "Done." in cleaned


def test_rejected_fences_still_strip_markup_from_display_text():
    text = "Trying:\n```read_file\nnotes/todo.md\n```\nDone."
    cleaned = strip_tool_blocks(text)
    assert "```read_file" not in cleaned
    assert "Trying:" in cleaned and "Done." in cleaned


# ---------------------------------------------------------------------------
# Rejected calls never execute; read-before-edit survives the textual path
# ---------------------------------------------------------------------------

def test_rejected_block_is_refused_by_dispatcher(monkeypatch):
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

    block = textual_function_call_to_tool_block(
        "read_file", "notes/todo.md", legacy_compat=False)
    assert block.tool_type == REJECTED_CALL_TOOL_TYPE

    def _boom(*_a, **_k):  # any dispatch attempt must be observable
        raise AssertionError("rejected block reached dispatch")

    monkeypatch.setattr("src.tool_execution._direct_fallback", _boom)

    async def _run():
        return await execute_tool_block(
            block, session_id="s", owner="o",
            security_context=NO_TOOL_SECURITY_CONTEXT,
        )

    _desc, result = asyncio.run(_run())
    assert result.get("blocked") is True
    assert result.get("execution_attempted") is False


def test_read_before_edit_enforced_for_textual_canonical_calls(tmp_path):
    from src.agent_runtime import read_baselines
    from src.agent_tools.filesystem_tools import EditFileTool, ReadFileTool

    store = read_baselines.ReadBaselineStore()
    token = read_baselines._store
    read_baselines._store = store
    try:
        target = tmp_path / "doc.md"
        target.write_text("alpha\nbeta\n", encoding="utf-8")
        ctx = {"owner": "textual-tester", "session_id": "textual-session"}

        # The exact block a textual canonical Edit call produces.
        edit_block = _blocks(
            '```Edit\n{"file_path": "%s", "old_string": "alpha", "new_string": "A"}\n```'
            % str(target))[0]
        assert edit_block.tool_type == "edit_file"
        denied = asyncio.run(EditFileTool().execute(edit_block.content, ctx))
        assert denied["exit_code"] == 1
        assert "read" in denied["error"].lower()

        # A textual canonical Read call records the baseline the edit needs.
        read_block = _blocks(f"```Read\n{target}\n```")[0]
        assert read_block.tool_type == "read_file"
        read_result = asyncio.run(ReadFileTool().execute(read_block.content, ctx))
        assert read_result["exit_code"] == 0

        allowed = asyncio.run(EditFileTool().execute(edit_block.content, ctx))
        assert allowed["exit_code"] == 0
        assert target.read_text(encoding="utf-8").startswith("A")
    finally:
        read_baselines._store = token
