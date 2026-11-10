"""M5.2 increment 3: compact-preview canonical tool representation.

The compact preview offers handler-keyed schemas internally and projects them
to the canonical Claude Code vocabulary only at the provider boundary. Model
calls come back under canonical names and are mapped back onto the handler
execution contracts before any policy or dispatch step; a call the registry
rejects is sealed as a truthful record and never dispatched.
"""
import json

from src.clean_agent_preview import (
    _canonical_wire_request,
    _registry_rejection_result,
    _unproject_preview_tool_calls,
    protocol_safe_tool_calls,
)
from src.tool_registry import (
    LegacyToolNameRejected,
    ToolRegistryError,
    resolve_model_call,
)


def _call(name, arguments):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _offered(*names):
    from src.tool_registry import REGISTRY, canonical_name_for_handler

    schemas = []
    for name in names:
        tool = REGISTRY.get(canonical_name_for_handler(name) or name)
        if tool is not None and canonical_name_for_handler(name) == name:
            schemas.append(tool.function_schema())
            continue
        schemas.append({
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
        })
    return schemas


def test_canonical_wire_call_unprojects_to_handler_contract():
    offered = _offered("read_file")
    proposed = [_call("Read", {"file_path": "notes/a.md", "offset": 2})]
    unprojected = _unproject_preview_tool_calls(proposed, offered)
    assert unprojected[0]["function"]["name"] == "read_file"
    assert json.loads(unprojected[0]["function"]["arguments"]) == {
        "path": "notes/a.md", "offset": 2,
    }
    # The call id is preserved so the tool result lands on the right row.
    assert unprojected[0]["id"] == "call_Read"


def test_handler_keyed_spelling_is_governed_like_any_legacy_spelling():
    """A handler-keyed native call is not a model-visible name.

    The wire advertises only the canonical vocabulary, so a model returning
    ``read_file`` is using a legacy spelling: the registry classifies it and
    seals a refusal instead of silently executing the alias.
    """
    offered = _offered("read_file")
    proposed = [_call("read_file", {"path": "a.md"})]
    unprojected = _unproject_preview_tool_calls(proposed, offered)
    assert unprojected[0].get("_registry_rejection")
    result = _registry_rejection_result(unprojected[0])
    assert result["blocked"] is True and result["execution_attempted"] is False
    assert "Read" in result["error"]


def test_registry_rejected_call_is_sealed_and_never_dispatched():
    offered = _offered("web_fetch")
    # The model was offered WebFetch on the wire; an alias spelling that is
    # not an offered name is classified by the registry and refused.
    proposed = [_call("webfetch", {"url": "https://example.com"})]
    unprojected = _unproject_preview_tool_calls(proposed, offered)
    sealed = unprojected[0].get("_registry_rejection")
    assert sealed  # the legacy spelling was refused on this governed surface
    result = _registry_rejection_result(unprojected[0])
    assert result["blocked"] is True
    assert result["execution_attempted"] is False


def test_unavailable_tool_call_is_sealed_never_simulated():
    offered = _offered("read_file")
    proposed = [_call("ToolSearch", {})]
    unprojected = _unproject_preview_tool_calls(proposed, offered)
    sealed = unprojected[0].get("_registry_rejection")
    assert sealed
    result = _registry_rejection_result(unprojected[0])
    assert result["blocked"] is True and result["execution_attempted"] is False
    assert "ToolSearch" in result["error"]


def test_inherited_and_mcp_calls_keep_their_identity():
    offered = _offered("manage_notes")
    proposed = [_call("manage_notes", {"action": "list"})]
    assert _unproject_preview_tool_calls(proposed, offered) == proposed
    mcp = [_call("mcp__srv__tool", {"x": 1})]
    assert _unproject_preview_tool_calls(mcp, _offered()) == mcp


def test_unprojected_history_stays_handler_keyed_on_the_wire():
    """History keeps the handler-keyed form; only the wire copy is canonical."""
    offered = _offered("read_file")
    history_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [_call("read_file", {"path": "a.md"})],
    }
    wire = _canonical_wire_request({
        "tools": offered,
        "messages": [history_message],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    })
    wire_names = [schema["function"]["name"] for schema in wire["tools"]]
    assert wire_names == ["Read"]
    wire_call = wire["messages"][0]["tool_calls"][0]["function"]
    assert wire_call["name"] == "Read"
    assert json.loads(wire_call["arguments"]) == {"file_path": "a.md"}
    assert wire["tool_choice"]["function"]["name"] == "Read"
    # The internal history object is never mutated.
    assert history_message["tool_calls"][0]["function"]["name"] == "read_file"


def test_protocol_safe_tool_calls_strips_private_rejection_marker():
    offered = _offered("read_file")
    proposed = _unproject_preview_tool_calls(
        [_call("read_file", {"path": "a.md"})], offered)
    safe = protocol_safe_tool_calls(proposed)
    assert all("_registry_rejection" not in call for call in safe)
    # The canonical identity and arguments stay in the persisted history.
    assert safe[0]["function"]["name"] == "read_file"


def test_non_registry_tool_choice_is_projected_unchanged():
    request = {
        "tools": _offered("manage_notes"),
        "messages": [],
        "tool_choice": {"type": "function", "function": {"name": "manage_notes"}},
    }
    wire = _canonical_wire_request(request)
    assert wire["tool_choice"]["function"]["name"] == "manage_notes"


def test_registry_rejection_result_shape_is_truthful():
    error = LegacyToolNameRejected(
        "'write_file' is not a tool name; use Write. "
        "Available built-in tools: Read, Write."
    )
    from src.tool_registry import rejected_block_content

    sealed = rejected_block_content("write_file", "{}", error)
    result = _registry_rejection_result({"_registry_rejection": sealed})
    assert result["blocked"] is True
    assert result["execution_attempted"] is False
    assert "Write" in result["error"]
    # Every registry error is a ToolRegistryError; a legacy native spelling
    # raises rather than resolving.
    assert isinstance(error, ToolRegistryError)
    try:
        resolve_model_call("write_file", "{}")
        raised = False
    except ToolRegistryError:
        raised = True
    assert raised
