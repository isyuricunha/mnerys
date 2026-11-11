"""M5.2 canonical tool registry: names, contracts, projection and validation.

The registry is the one provider-independent model-visible tool vocabulary.
It owns names and argument contracts only — never execution, never authority —
so every assertion here checks the boundary: the bijection to internal
handlers, the immutable catalog, honest schema projection and fail-closed
inbound validation.
"""
import json

import pytest

from src.tool_registry import (
    CANONICAL_NAMES,
    HANDLER_IDS,
    REGISTRY,
    REJECTED_CALL_TOOL_TYPE,
    UNAVAILABLE_NAMES,
    CanonicalTool,
    InvalidToolArguments,
    LegacyToolNameRejected,
    ResolvedCall,
    ToolRegistryError,
    ToolUnavailable,
    canonical_name_for_handler,
    handler_for_canonical,
    legacy_names,
    model_visible_names,
    project_messages_for_wire,
    project_tools_for_wire,
    public_tool_name,
    rejected_block_content,
    rejected_block_result,
    rejection_result,
    resolve_model_call,
)
from src.tool_types import TOOL_TAGS


EXPECTED_CANONICAL = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "ToolSearch", "Skill")


# ---------------------------------------------------------------------------
# Catalog shape and immutability
# ---------------------------------------------------------------------------

def test_canonical_catalog_matches_the_fixed_vocabulary():
    assert CANONICAL_NAMES == EXPECTED_CANONICAL
    assert set(REGISTRY) == set(EXPECTED_CANONICAL)
    # Claude Code tools without a real implementation stay unavailable.
    # M5.2 increment 5: every Claude Code built-in name is now backed by a
    # real implementation, so nothing is unavailable.
    assert set(UNAVAILABLE_NAMES) == set()


def test_registry_is_immutable():
    assert type(REGISTRY).__name__ == "mappingproxy"
    with pytest.raises(TypeError):
        REGISTRY["Backdoor"] = CanonicalTool("Backdoor", "x", "d", ())
    tool = REGISTRY["Read"]
    with pytest.raises(Exception):
        tool.name = "Renamed"


def test_every_canonical_tool_maps_to_a_real_internal_handler():
    # One-to-one: canonical name <-> handler id, and each handler is an
    # executable tag the inherited dispatcher actually knows.
    assert len(set(HANDLER_IDS)) == len(CANONICAL_NAMES)
    assert {handler_for_canonical(n) for n in CANONICAL_NAMES} == set(HANDLER_IDS)
    for handler in HANDLER_IDS:
        assert handler in TOOL_TAGS, handler
        assert canonical_name_for_handler(handler) is not None


def test_function_schemas_are_strict_and_honest():
    for tool in REGISTRY.values():
        schema = tool.function_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == tool.name
        assert fn["description"].strip()
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        required = params["required"]
        assert required == [p.name for p in tool.params if p.required]
        for param in tool.params:
            assert param.name in params["properties"]
            assert params["properties"][param.name]["type"] in {"string", "integer", "boolean"}
            if param.handler_arg is None:
                # Annotation-only arguments must disclose on the wire that
                # they never change behavior.
                description = params["properties"][param.name]["description"]
                assert "Mnerys extension" in description or (
                    "not forwarded" in description or "not executed" in description
                )


def test_no_legacy_handler_ids_are_advertised():
    advertised = set(model_visible_names([t.function_schema() for t in REGISTRY.values()]))
    assert advertised == set(EXPECTED_CANONICAL)
    assert not (advertised & legacy_names())


# ---------------------------------------------------------------------------
# Inbound validation
# ---------------------------------------------------------------------------

def test_canonical_call_resolves_to_handler_with_translated_arguments():
    resolved = resolve_model_call("Read", json.dumps({"file_path": "a.py", "offset": 3, "limit": 10}))
    assert isinstance(resolved, ResolvedCall)
    assert resolved.governed is True
    assert resolved.handler == "read_file"
    assert resolved.arguments == {"path": "a.py", "offset": 3, "limit": 10}


def test_canonical_call_accepts_dict_arguments_and_empty_arguments():
    resolved = resolve_model_call("Bash", {"command": "ls -la"})
    assert resolved.governed and resolved.handler == "bash"
    assert resolved.arguments == {"command": "ls -la"}
    # An omitted arguments field means no arguments at all.
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Bash", None)


@pytest.mark.parametrize("name", ["read_file", "write_file", "edit_file", "glob", "grep", "bash", "web_fetch", "web_search"])
def test_legacy_handler_ids_are_rejected_for_new_model_calls(name):
    with pytest.raises(LegacyToolNameRejected) as excinfo:
        resolve_model_call(name, "{}")
    assert canonical_name_for_handler(name) in str(excinfo.value)


@pytest.mark.parametrize("name", ["shell", "cat", "read", "write", "fetch", "search", "websearch", "webfetch", "run", "terminal", "command", "execute"])
def test_inherited_alias_spellings_are_rejected_too(name):
    with pytest.raises(LegacyToolNameRejected):
        resolve_model_call(name, "{}")


def test_every_canonical_name_resolves_to_a_real_handler():
    # M5.2 increment 5: the registry advertises only capabilities backed by
    # working implementations — every canonical name, ToolSearch and Skill
    # included, must map to a registered handler id that round-trips.
    from src.agent_tools import TOOL_HANDLERS

    for name in CANONICAL_NAMES:
        tool = REGISTRY[name]
        assert tool.handler in TOOL_HANDLERS, name
        assert canonical_name_for_handler(tool.handler) == name
    resolved = resolve_model_call("Skill", "{}")
    assert resolved.governed is True and resolved.handler == "activate_skill"
    resolved = resolve_model_call("ToolSearch", json.dumps({"pattern": "weather"}))
    assert resolved.governed is True and resolved.handler == "tool_search"


def test_non_registry_tools_pass_through_unchanged():
    for name in ("python", "private_browser", "manage_notes", "mcp__github__list_issues"):
        resolved = resolve_model_call(name, json.dumps({"any": "shape"}))
        assert resolved.governed is False
        assert resolved.handler == name
        assert resolved.arguments is None


def test_unknown_arguments_are_rejected_not_dropped():
    with pytest.raises(InvalidToolArguments) as excinfo:
        resolve_model_call("Glob", {"pattern": "*.py", "silent": True})
    assert "silent" in str(excinfo.value)


def test_unsupported_claude_code_parameters_are_rejected_explicitly():
    with pytest.raises(InvalidToolArguments) as grep_mode:
        resolve_model_call("Grep", {"pattern": "x", "output_mode": "content"})
    assert "output_mode" in str(grep_mode.value)
    with pytest.raises(InvalidToolArguments) as bash_timeout:
        resolve_model_call("Bash", {"command": "ls", "timeout": 5})
    assert "timeout" in str(bash_timeout.value)


def test_required_missing_and_type_errors_reject():
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Edit", {"file_path": "a", "old_string": "b"})
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Read", {"file_path": "a", "limit": "many"})
    # bool is not an integer
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Read", {"file_path": "a", "offset": True})
    # Empty content is a legitimate Write (creates an empty file); an empty
    # required path is not a legitimate Read.
    resolved = resolve_model_call("Write", {"file_path": "a", "content": ""})
    assert resolved.arguments == {"path": "a", "content": ""}
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Read", {"file_path": "   "})
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("WebSearch", {"query": "x"})


def test_enum_and_annotated_only_arguments_behave_as_contracted():
    resolved = resolve_model_call("WebSearch", {"query": "rust releases", "time_filter": "week"})
    assert resolved.arguments == {"query": "rust releases", "time_filter": "week"}
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("WebSearch", {"query": "rust releases", "time_filter": "decade"})
    # Annotation-only parameters are accepted, never forwarded.
    resolved = resolve_model_call("Bash", {"command": "ls", "description": "list files"})
    assert resolved.arguments == {"command": "ls"}
    resolved = resolve_model_call("WebFetch", {"url": "https://example.com", "prompt": "find prices"})
    assert resolved.arguments == {"url": "https://example.com"}


def test_malformed_json_arguments_reject():
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Read", "{not json")
    with pytest.raises(InvalidToolArguments):
        resolve_model_call("Read", ["a.py"])


# ---------------------------------------------------------------------------
# Outbound projection
# ---------------------------------------------------------------------------

def _handler_schema(name):
    return {"type": "function", "function": {"name": name, "description": "legacy", "parameters": {}}}


def _mcp_schema():
    return {"type": "function", "function": {"name": "mcp__github__list_issues", "parameters": {"type": "object"}}}


def test_handler_schemas_project_to_canonical_names():
    schemas = [_handler_schema("read_file"), _handler_schema("bash")]
    out = project_tools_for_wire(schemas)
    assert model_visible_names(out) == ["Read", "Bash"]
    # The projected schema is the registry's own contract, not the legacy one.
    assert out[0] == REGISTRY["Read"].function_schema()


def test_projection_never_adds_a_tool_that_was_not_admitted():
    schemas = [_handler_schema("manage_notes"), _mcp_schema()]
    out = project_tools_for_wire(schemas)
    assert out == schemas, "untouched inventory must pass through unchanged"
    assert model_visible_names(out) == ["manage_notes", "mcp__github__list_issues"]


def test_projection_preserves_mcp_and_inherited_tools():
    mcp = _mcp_schema()
    inherited = _handler_schema("python")
    out = project_tools_for_wire([_handler_schema("read_file"), mcp, inherited])
    assert out[1] is mcp
    assert out[2] is inherited
    assert model_visible_names(out) == ["Read", "mcp__github__list_issues", "python"]


def test_caller_supplied_canonical_schema_wins_without_duplicates():
    canonical = REGISTRY["Read"].function_schema()
    out = project_tools_for_wire([_handler_schema("read_file"), canonical])
    assert model_visible_names(out) == ["Read"]
    assert out[0] is canonical


def test_empty_projection_is_identity():
    assert project_tools_for_wire(None) is None
    assert project_tools_for_wire([]) == []


# ---------------------------------------------------------------------------
# History projection (historical transcript compatibility)
# ---------------------------------------------------------------------------

def test_persisted_history_projects_to_canonical_for_the_wire():
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py", "offset": 2})},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]
    out = project_messages_for_wire(history)
    assert out is not history
    call = out[1]["tool_calls"][0]["function"]
    assert call["name"] == "Read"
    assert json.loads(call["arguments"]) == {"file_path": "a.py", "offset": 2}
    # Tool results are untouched, and the caller's history is not mutated.
    assert out[2] is history[2]
    assert history[1]["tool_calls"][0]["function"]["name"] == "read_file"


def test_history_projection_is_identity_without_registry_calls():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": [{
            "id": "c", "type": "function",
            "function": {"name": "manage_notes", "arguments": "{}"},
        }]},
    ]
    out = project_messages_for_wire(history)
    assert out is history


def test_history_projection_translates_only_registry_tools_in_mixed_calls():
    history = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "x", "content": "hi"})}},
            {"id": "b", "type": "function", "function": {"name": "mcp__x__y", "arguments": "{}"}},
        ],
    }]
    out = project_messages_for_wire(history)
    names = [c["function"]["name"] for c in out[0]["tool_calls"]]
    assert names == ["Write", "mcp__x__y"]
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"file_path": "x", "content": "hi"}


def test_wire_arguments_round_trip_through_inbound_resolution():
    # Outbound translation (handler -> canonical) and inbound translation
    # (canonical -> handler) are inverses: a historical call re-validated on
    # the canonical surface keeps its exact operation.
    tool = REGISTRY["Grep"]
    wire = project_tools_for_wire([_handler_schema("grep")])[0]["function"]["name"]
    assert wire == "Grep"
    resolved = resolve_model_call("Grep", {"pattern": "x", "-i": True, "head_limit": 5})
    assert resolved.arguments == {"pattern": "x", "ignore_case": True, "max_results": 5}
    assert resolved.handler == tool.handler


# ---------------------------------------------------------------------------
# Rejection records
# ---------------------------------------------------------------------------

def test_rejection_records_are_truthful_and_inert():
    envelope = rejection_result("Read", LegacyToolNameRejected("'read_file' is not a tool name"))
    assert envelope["blocked"] is True
    assert envelope["execution_attempted"] is False
    assert envelope["failure_kind"] == "tool_registry_rejected"
    assert envelope["rejection_code"] == "legacy_tool_name"

    content = rejected_block_content("Read", json.dumps({"file_path": "a"}), LegacyToolNameRejected("nope"))
    parsed = rejected_block_result(content)
    assert parsed["blocked"] is True
    assert parsed["rejected_tool"] == "Read"
    # Corrupt content still yields a truthful, non-executing envelope.
    assert rejected_block_result("not json")["blocked"] is True


def test_public_tool_name_is_identity_outside_the_registry():
    assert public_tool_name("read_file") == "Read"
    assert public_tool_name("manage_notes") == "manage_notes"
    assert public_tool_name("mcp__x__y") == "mcp__x__y"


def test_rejected_pseudo_type_is_not_a_real_tool():
    assert REJECTED_CALL_TOOL_TYPE not in TOOL_TAGS
    assert REJECTED_CALL_TOOL_TYPE not in REGISTRY
