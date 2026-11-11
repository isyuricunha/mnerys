"""M5.2 canonical, provider-independent model-visible tool registry.

One fixed catalog of built-in tools, named and shaped after Claude Code's
public tool contracts (``Read``, ``Write``, ``Edit``, ``Glob``, ``Grep``,
``Bash``, ``WebFetch``, ``WebSearch``, ``ToolSearch``, ``Skill``). Every model
on every provider protocol sees exactly these names, descriptions and argument
contracts.

Boundary, not an engine
-----------------------
This module owns **names and argument contracts only**. It never executes
anything and never grants authority. Each canonical tool maps one-to-one onto
an existing, hardened internal handler id (``Read`` -> ``read_file``). The
inherited engine -- request authority, tool policy, exact-operation identity,
approval fingerprints, resource bindings, effect digests and the dispatcher --
keeps keying on that handler id, so a canonical call is translated *once*, at
the wire boundary, and then travels the single existing Gateway execution path.
The mapping is a bijection, so identity stays exact and nothing is widened.

* Outbound: :func:`project_tools_for_wire` / :func:`project_messages_for_wire`
  serialise the already admitted schema set under canonical names. They never
  add a tool that was not admitted, never invent one, and leave every other
  inherited tool and every MCP tool (server-defined names and schemas)
  untouched.
* Inbound: :func:`resolve_model_call` validates a model-generated call against
  the registry *before* any other guard. Legacy handler ids and their alias
  spellings are rejected for registry-backed tools; unknown arguments are
  rejected rather than silently dropped; unavailable tools are refused.

This is distinct from Anthropic's versioned API-native ``bash_*`` /
``text_editor_*`` tool types, which are never substituted for these contracts.

Claude Code parameters the inherited handlers cannot honour are *not*
advertised and are rejected explicitly with an actionable message; they are
never simulated.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence


class ToolRegistryError(ValueError):
    """A model-generated call violated the canonical registry contract."""

    code = "invalid_tool_call"


# Inert tool_type for a model-generated call the registry rejected. A block
# with this type is a *record*, never an operation: it carries the truthful
# rejection envelope as its content and must never be dispatched to a real
# handler. Guards in the dispatcher and the agent loop enforce that.
REJECTED_CALL_TOOL_TYPE = "tool_call_rejected"


def rejection_result(tool: str, error: "ToolRegistryError | str") -> dict:
    """Truthful, non-executing result envelope for one rejected call.

    Shaped like the dispatcher's blocked results so downstream provenance,
    event framing (``tool.blocked``) and UI rendering treat it exactly like
    any other refused operation. ``execution_attempted`` stays False because
    nothing ran; the error text is the registry's own actionable message.
    """
    message = str(getattr(error, "args", (error,))[0] if error else "") or str(error or "")
    code = getattr(error, "code", "invalid_tool_call")
    return {
        "error": message,
        "exit_code": 1,
        "blocked": True,
        "execution_attempted": False,
        "failure_kind": "tool_registry_rejected",
        "rejection_code": code,
        "rejected_tool": str(tool or ""),
        "output": message,
    }


def rejected_block_content(tool: str, arguments: Any, error: "ToolRegistryError | str") -> str:
    """Content of an inert :data:`REJECTED_CALL_TOOL_TYPE` block.

    Carries the model's own call (echoed for the continuation history) plus
    the sealed truthful result envelope. Nothing here is ever dispatched.
    """
    if isinstance(arguments, str):
        echoed = arguments
    elif arguments is None:
        echoed = ""
    else:
        try:
            echoed = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            echoed = str(arguments)
    return json.dumps({
        "requested_tool": str(tool or ""),
        "requested_arguments": echoed,
        "result": rejection_result(tool, error),
    }, ensure_ascii=False)


def rejected_block_result(content: Any) -> dict:
    """Return the stored truthful result envelope of a rejected block."""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        parsed = None
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if isinstance(result, dict):
        return result
    return rejection_result(
        str((parsed or {}).get("requested_tool", "")) if isinstance(parsed, dict) else "",
        "The tool call was rejected before execution.",
    )


class LegacyToolNameRejected(ToolRegistryError):
    code = "legacy_tool_name"


class ToolUnavailable(ToolRegistryError):
    code = "tool_unavailable"


class InvalidToolArguments(ToolRegistryError):
    code = "invalid_tool_arguments"


@dataclass(frozen=True)
class Param:
    """One model-visible argument.

    ``handler_arg`` is the internal handler's argument name. ``None`` marks an
    annotation-only argument: accepted so a Claude Code style call is not
    rejected, but never forwarded and never claimed to change behaviour.
    """

    name: str
    schema: Mapping[str, Any]
    required: bool = False
    handler_arg: Optional[str] = None
    allow_empty: bool = False
    extension: bool = False


@dataclass(frozen=True)
class CanonicalTool:
    name: str
    handler: str
    description: str
    params: tuple[Param, ...]
    unsupported: tuple[tuple[str, str], ...] = ()

    def function_schema(self) -> dict:
        properties: dict[str, Any] = {}
        for param in self.params:
            fragment = copy.deepcopy(dict(param.schema))
            description = str(fragment.get("description", ""))
            if param.extension:
                description = (
                    "Mnerys extension (same on every provider): " + description
                ).strip()
            elif param.handler_arg is None:
                # Annotation-only argument: honest on the wire that it never
                # changes behavior, unless the description already discloses it.
                if "not forwarded" not in description and "not executed" not in description:
                    description = (
                        description + " (recorded for context; not forwarded.)"
                    ).strip()
            fragment["description"] = description
            properties[param.name] = fragment
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [p.name for p in self.params if p.required],
                    "additionalProperties": False,
                },
            },
        }


def _s(description: str, **extra: Any) -> dict:
    return {"type": "string", "description": description, **extra}


def _i(description: str, **extra: Any) -> dict:
    return {"type": "integer", "description": description, "minimum": 1, **extra}


def _b(description: str) -> dict:
    return {"type": "boolean", "description": description}


_UNSUPPORTED_GREP = (
    "Not supported by this tool; it always returns file:line:match lines."
)

_TOOLS: tuple[CanonicalTool, ...] = (
    CanonicalTool(
        name="Read",
        handler="read_file",
        description=(
            "Read a local file. Use file_path for the file; optionally pass "
            "offset (1-based line to start at) and limit (maximum number of "
            "lines). This does not download URLs; use WebFetch for http(s) "
            "resources."
        ),
        params=(
            Param("file_path", _s("Path of the file to read."), True, "path"),
            Param("offset", _i("1-based line to start reading from."), False, "offset"),
            Param("limit", _i("Maximum number of lines to read from offset."), False, "limit"),
        ),
    ),
    CanonicalTool(
        name="Write",
        handler="write_file",
        description=(
            "Write one complete file to disk. Use it to create a new file; "
            "use Edit to change part of an existing file. Issue several "
            "Write calls in one response for several independent files."
        ),
        params=(
            Param("file_path", _s("Path of the file to write."), True, "path"),
            Param("content", _s("Complete file content."), True, "content", allow_empty=True),
        ),
    ),
    CanonicalTool(
        name="Edit",
        handler="edit_file",
        description=(
            "Edit an existing file on disk by exact string replacement. "
            "old_string must match the file exactly and be unique unless "
            "replace_all is true. Use Write to create a new file."
        ),
        params=(
            Param("file_path", _s("Path of the file to edit."), True, "path"),
            Param("old_string", _s("Exact text to replace, including indentation."), True, "old_string"),
            Param("new_string", _s("Replacement text."), True, "new_string", allow_empty=True),
            Param("replace_all", _b("Replace every occurrence instead of requiring a unique match."), False, "replace_all"),
        ),
    ),
    CanonicalTool(
        name="Glob",
        handler="glob",
        description=(
            "Find files by glob pattern (recursive), newest first, for "
            "example '**/*.py'. Prefer it over shell find/ls."
        ),
        params=(
            Param("pattern", _s("Glob pattern to match files against."), True, "pattern"),
            Param("path", _s("Base directory (defaults to the project root)."), False, "path"),
        ),
    ),
    CanonicalTool(
        name="Grep",
        handler="grep",
        description=(
            "Search file contents for a regular expression across a "
            "directory tree. Returns file:line:match lines. Prefer it over "
            "shell grep/rg."
        ),
        params=(
            Param("pattern", _s("Regular expression to search for."), True, "pattern"),
            Param("path", _s("File or directory to search (defaults to the project root)."), False, "path"),
            Param("glob", _s("Only search files matching this glob, for example '*.py'."), False, "glob"),
            Param("-i", _b("Case-insensitive match."), False, "ignore_case"),
            Param("head_limit", _i("Maximum number of matches to return."), False, "max_results"),
        ),
        unsupported=(
            ("output_mode", _UNSUPPORTED_GREP),
            ("-n", _UNSUPPORTED_GREP),
            ("-A", _UNSUPPORTED_GREP),
            ("-B", _UNSUPPORTED_GREP),
            ("-C", _UNSUPPORTED_GREP),
            ("context", _UNSUPPORTED_GREP),
            ("type", _UNSUPPORTED_GREP),
            ("multiline", _UNSUPPORTED_GREP),
            ("offset", _UNSUPPORTED_GREP),
        ),
    ),
    CanonicalTool(
        name="Bash",
        handler="bash",
        description=(
            "Run a shell command. Prefer a dedicated tool when one fits "
            "(Read, Write, Edit, Glob, Grep); use Bash for installs, git, "
            "builds, running programs and system information."
        ),
        params=(
            Param("command", _s("The shell command to execute."), True, "command"),
            Param("description", _s("Short human-readable note about what the command does."), False, None),
        ),
        unsupported=(
            ("timeout", "Per-call timeouts are not supported by this Bash tool."),
            ("run_in_background", "Background execution is not available through Bash yet."),
            ("dangerouslyDisableSandbox", "Sandbox overrides are not available."),
        ),
    ),
    CanonicalTool(
        name="WebFetch",
        handler="web_fetch",
        description=(
            "Fetch and read one known http(s) URL and return its content. "
            "No extraction model is run: prompt is recorded as context only "
            "and you apply it to the returned content yourself. For a long "
            "page or PDF pass query to select matching passages; if the "
            "result reports partial content, call again with full=true."
        ),
        params=(
            Param("url", _s("The URL to fetch (http or https)."), True, "url"),
            Param("prompt", _s("What you want from the page (context only; not executed)."), False, None),
            Param("full", _b("Raise the download budget after a partial-content result."), False, "full", extension=True),
            Param("query", _s("Comma-separated terms used to select matching passages from long documents."), False, "query", extension=True),
        ),
    ),
    CanonicalTool(
        name="WebSearch",
        handler="web_search",
        description=(
            "Search the web through the configured search backend and "
            "return titles, URLs and snippets. Use WebFetch to read a page."
        ),
        params=(
            Param("query", _s("Search query.", minLength=2), True, "query"),
            Param(
                "time_filter",
                {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "Optional publication-date window for recent articles.",
                },
                False, "time_filter", extension=True,
            ),
        ),
        unsupported=(
            ("allowed_domains", "Domain allow-lists are not supported by this search backend."),
            ("blocked_domains", "Domain block-lists are not supported by this search backend."),
        ),
    ),
    CanonicalTool(
        name="ToolSearch",
        handler="tool_search",
        description=(
            "Search this turn's authorized tool inventory (built-ins, "
            "inherited tools and connected MCP tools) by Python regular "
            "expression over tool names, descriptions, argument names and "
            "argument descriptions. Returns matching tools with their names "
            "and schemas and makes them invocable for the rest of this turn. "
            "Discovery is not extra permission: denied or disabled tools are "
            "never searchable, and every invocation still passes the normal "
            "admission and approval checks. Use it when a capability seems "
            "missing or the tool list is large."
        ),
        params=(
            Param("pattern", _s("Python re.search() pattern, case-insensitive; matches tool names, descriptions, argument names and argument descriptions. Maximum 200 characters.", maxLength=200), True, "pattern"),
            Param("limit", _i("Maximum number of tools to return (default 5).", maximum=20), False, "limit"),
        ),
    ),
    CanonicalTool(
        name="Skill",
        handler="activate_skill",
        description=(
            "Activate one installed skill and load its instructions, or list "
            "the skills available to you. Skills are scoped procedures from "
            "the skill library (published and approved entries; built-ins are "
            "always available). Call with no arguments for a compact index, "
            "or pass name to load that skill's SKILL.md; pass path to read "
            "one supporting reference file under the skill's directory. "
            "Skill content is instructions to apply through your normal "
            "authorized tools — it never grants permissions by itself."
        ),
        params=(
            Param("name", _s("Registered name of the installed skill to activate (see the skill index). Omit to list available skills.", maxLength=80), False, "name"),
            Param("path", _s("Optional path of one supporting file under the skill's directory (for example 'references/api-notes.md').", maxLength=200), False, "path"),
        ),
    ),
)

# Claude Code tools that exist upstream but have no real implementation here.
# They are never advertised and any call to them is refused, never simulated.
# (ToolSearch and Skill moved to real implementations in M5.2 increment 5.)
_UNAVAILABLE: MappingProxyType = MappingProxyType({})

REGISTRY: Mapping[str, CanonicalTool] = MappingProxyType({t.name: t for t in _TOOLS})
_BY_HANDLER: Mapping[str, CanonicalTool] = MappingProxyType({t.handler: t for t in _TOOLS})
_BY_CANONICAL: Mapping[str, CanonicalTool] = MappingProxyType({t.name: t for t in _TOOLS})
CANONICAL_NAMES: tuple[str, ...] = tuple(t.name for t in _TOOLS)
HANDLER_IDS: frozenset[str] = frozenset(_BY_HANDLER)
UNAVAILABLE_NAMES: frozenset[str] = frozenset(_UNAVAILABLE)


def policy_spellings(name: str) -> tuple[str, ...]:
    """All policy-equivalent spellings of a tool name (M5.2 increment 6).

    THE one normalization boundary for deny/allow interpretation. A registry
    built-in may be named in a policy rule either by its internal handler id
    (``bash``, ``read_file``) or by its canonical model-visible name
    (``Bash``, ``Read``) — both spellings address the same tool and every
    policy gate must treat them identically. MCP server-defined names and
    unknown/inherited names alias only to themselves and are never
    translated (case-sensitive, so an unrelated inherited tool is not swept
    into a canonical denial).
    """
    candidate = str(name or "")
    if not candidate:
        return ("",)
    if candidate.startswith("mcp__"):
        return (candidate,)
    canonical = _BY_CANONICAL.get(candidate)
    if canonical is not None:
        return (candidate, canonical.handler)
    tool = _BY_HANDLER.get(candidate)
    if tool is not None:
        return (candidate, tool.name)
    return (candidate,)


def canonical_name_for_handler(handler: str) -> Optional[str]:
    tool = _BY_HANDLER.get(str(handler or ""))
    return tool.name if tool else None


def handler_for_canonical(name: str) -> Optional[str]:
    tool = REGISTRY.get(str(name or ""))
    return tool.handler if tool else None


def public_tool_name(name: str) -> str:
    """Model/client-facing name for an internal handler id (identity otherwise)."""
    return canonical_name_for_handler(name) or str(name or "")


@lru_cache(maxsize=1)
def legacy_names() -> frozenset[str]:
    """Handler ids and inherited alias spellings that resolve to a registry tool."""
    names = set(HANDLER_IDS)
    try:  # inherited alias table; imported lazily to avoid an import cycle
        from src.tool_parsing import _TOOL_NAME_MAP

        names.update(a for a, h in _TOOL_NAME_MAP.items() if h in HANDLER_IDS)
    except Exception:  # pragma: no cover - fall back to the exact handler ids
        pass
    return frozenset(names - set(CANONICAL_NAMES))


@lru_cache(maxsize=1)
def _canonical_lower_index() -> dict[str, str]:
    return {name.lower(): name for name in CANONICAL_NAMES}


@lru_cache(maxsize=1)
def _unavailable_lower_index() -> dict[str, str]:
    return {name.lower(): message for name, message in _UNAVAILABLE.items()}


def unavailable_message_for(name: str) -> Optional[str]:
    """Refusal message for an unavailable Claude Code tool, exact spelling.

    Exact-case only on purpose: an inherited alias that happens to collide
    case-insensitively (``skill`` -> ``manage_skills``) keeps working outside
    the canonical vocabulary; only the model writing the Claude Code spelling
    itself is told the tool is unavailable.
    """
    return _UNAVAILABLE.get(str(name or "").strip())


def unavailable_message_for_spelling(name: str) -> Optional[str]:
    """Case-insensitive :func:`unavailable_message_for`.

    Used by channels where a case-variant spelling was never an executable
    capability (fenced tags), so any casing of an unavailable name gets the
    same truthful refusal.
    """
    return _unavailable_lower_index().get(str(name or "").strip().lower())


def registry_spelling_for(name: str) -> Optional[str]:
    """The canonical name a model-spelled tool name resolves to, if any.

    Accepts the exact canonical spelling, a case variant of it, a legacy
    handler id, or an inherited alias spelling — anything that identifies one
    of the registry's tools. Returns None for names outside the registry
    (inherited tools, MCP servers, request-scoped external schemas). This is
    the single resolution primitive for the textual channels: parsers use it
    to admit canonical spellings, to reject legacy aliases on governed turns,
    and to leave everything else untouched.
    """
    value = str(name or "").strip()
    if not value:
        return None
    if value in CANONICAL_NAMES:
        return value
    folded = value.lower()
    canonical = _canonical_lower_index().get(folded)
    if canonical is not None:
        return canonical
    try:  # alias table; lazily imported to keep the registry import-light
        from src.tool_parsing import _TOOL_NAME_MAP
    except Exception:  # pragma: no cover
        return None
    handler = _TOOL_NAME_MAP.get(folded) or (value if value in HANDLER_IDS else None)
    if handler in HANDLER_IDS:
        return canonical_name_for_handler(handler)
    return None


@dataclass(frozen=True)
class ResolvedCall:
    """Outcome of validating one model-generated call.

    ``governed`` is True for registry tools. Then ``handler`` is the internal
    handler id and ``arguments`` the translated, validated internal arguments.
    For every other tool (inherited non-registry tools, MCP) the call is not
    governed here: ``handler`` equals the model name and ``arguments`` is None.
    """

    model_name: str
    handler: str
    arguments: Optional[dict]
    governed: bool


def _schema_name(schema: Any) -> Optional[str]:
    if not isinstance(schema, Mapping):
        return None
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "") or None
    return None


def project_tools_for_wire(tools: Optional[Sequence[Mapping[str, Any]]]):
    """Serialise the admitted schema set under canonical names.

    Only handlers that are *already present* are projected, so availability,
    policy and owner restrictions decided upstream are preserved exactly. A
    canonical tool already supplied by the caller (request-scoped external
    tools) wins, so the wire never carries a duplicate name.
    """
    if not tools:
        return tools
    present = {_schema_name(t) for t in tools}
    out = []
    for tool in tools:
        name = _schema_name(tool)
        registry_tool = _BY_HANDLER.get(name) if name else None
        if registry_tool is None or registry_tool.name in present:
            if registry_tool is not None and registry_tool.name in present:
                continue  # caller already supplied the canonical schema
            out.append(tool)
            continue
        out.append(registry_tool.function_schema())
    return out


def _to_wire_arguments(tool: CanonicalTool, arguments: Mapping[str, Any]) -> dict:
    reverse = {p.handler_arg: p.name for p in tool.params if p.handler_arg}
    return {reverse.get(key, key): value for key, value in arguments.items()}


def project_tool_choice_for_wire(choice: Any):
    """Project a handler-keyed tool_choice onto its canonical wire name.

    A forced tool_choice must name a tool exactly as the provider sees it: the
    schemas leave this boundary under canonical names, so a handler-keyed
    ``{"function": {"name": "web_search"}}`` constraint would no longer match
    the offered ``WebSearch`` schema. Non-mapping choices ('auto', 'required',
    'none') and non-registry tools are returned unchanged.
    """
    if not isinstance(choice, Mapping):
        return choice
    function = choice.get("function")
    if not isinstance(function, Mapping):
        return choice
    canonical = canonical_name_for_handler(str(function.get("name") or ""))
    if canonical is None:
        return choice
    return {**dict(choice), "function": {**dict(function), "name": canonical}}


def project_messages_for_wire(messages: Optional[Sequence[Mapping[str, Any]]]):
    """Rename registry handler ids in prior assistant tool calls for the wire.

    History keeps its stored (handler id) form for rendering and replay; only
    the copy sent to a provider is projected, so a model never sees legacy
    names next to a canonical tool set. Returns the input unchanged when no
    message needs a change.
    """
    if not messages:
        return messages
    changed = False
    out = []
    for message in messages:
        if not isinstance(message, Mapping):
            out.append(message)
            continue
        calls = message.get("tool_calls")
        name = message.get("name")
        needs_call = isinstance(calls, list) and any(
            isinstance(c, Mapping)
            and canonical_name_for_handler((c.get("function") or {}).get("name"))
            for c in calls
        )
        needs_name = isinstance(name, str) and canonical_name_for_handler(name) is not None
        if not (needs_call or needs_name):
            out.append(message)
            continue
        changed = True
        projected = dict(message)
        if needs_name:
            projected["name"] = canonical_name_for_handler(name)
        if needs_call:
            new_calls = []
            for call in calls:
                function = dict((call.get("function") or {})) if isinstance(call, Mapping) else {}
                tool = _BY_HANDLER.get(function.get("name"))
                if tool is not None:
                    function["name"] = tool.name
                    raw = function.get("arguments")
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    if isinstance(parsed, Mapping):
                        wire_args = _to_wire_arguments(tool, parsed)
                        function["arguments"] = (
                            json.dumps(wire_args, ensure_ascii=False)
                            if isinstance(raw, str) else wire_args
                        )
                new_call = dict(call) if isinstance(call, Mapping) else call
                if isinstance(new_call, dict):
                    new_call["function"] = function
                new_calls.append(new_call)
            projected["tool_calls"] = new_calls
        out.append(projected)
    return out if changed else messages


def _parse_arguments(name: str, arguments: Any) -> dict:
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        return {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidToolArguments(f"{name}: arguments are not valid JSON") from exc
    if not isinstance(arguments, Mapping):
        raise InvalidToolArguments(f"{name}: arguments must be a JSON object")
    return dict(arguments)


def _check_value(tool: CanonicalTool, param: Param, value: Any) -> None:
    kind = param.schema.get("type")
    label = f"{tool.name}.{param.name}"
    if kind == "string":
        if not isinstance(value, str):
            raise InvalidToolArguments(f"{label} must be a string")
        if not param.allow_empty and not value.strip():
            raise InvalidToolArguments(f"{label} must not be empty")
        minimum = param.schema.get("minLength")
        if minimum and len(value.strip()) < minimum:
            raise InvalidToolArguments(f"{label} must be at least {minimum} characters")
        # maxLength is advertised in the projected schema, so it must be
        # enforced here too — an advertised bound the validator ignores would
        # let oversized arguments through (M5.2 increment 5).
        maximum = param.schema.get("maxLength")
        if maximum is not None and len(value) > maximum:
            raise InvalidToolArguments(f"{label} must be at most {maximum} characters")
        allowed = param.schema.get("enum")
        if allowed and value not in allowed:
            raise InvalidToolArguments(f"{label} must be one of {', '.join(allowed)}")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidToolArguments(f"{label} must be an integer")
        minimum = param.schema.get("minimum")
        if minimum is not None and value < minimum:
            raise InvalidToolArguments(f"{label} must be >= {minimum}")
        maximum = param.schema.get("maximum")
        if maximum is not None and value > maximum:
            raise InvalidToolArguments(f"{label} must be <= {maximum}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise InvalidToolArguments(f"{label} must be a boolean")


def resolve_model_call(name: Any, arguments: Any) -> ResolvedCall:
    """Validate one model-generated tool call against the registry.

    Raises :class:`ToolRegistryError` subclasses; the caller must treat any
    exception as a rejected call and must not fall back to another spelling.
    """
    model_name = str(name or "").strip()
    if model_name in _UNAVAILABLE:
        raise ToolUnavailable(_UNAVAILABLE[model_name])
    tool = REGISTRY.get(model_name)
    if tool is None:
        if model_name in legacy_names():
            replacement = canonical_name_for_handler(model_name)
            hint = f"; use {replacement}" if replacement else ""
            raise LegacyToolNameRejected(
                f"'{model_name}' is not a tool name{hint}. "
                f"Available built-in tools: {', '.join(CANONICAL_NAMES)}."
            )
        return ResolvedCall(model_name, model_name, None, False)

    args = _parse_arguments(tool.name, arguments)
    declared = {p.name: p for p in tool.params}
    reasons = dict(tool.unsupported)
    for key in args:
        if key in declared:
            continue
        if key in reasons:
            raise InvalidToolArguments(f"{tool.name}.{key} is unsupported: {reasons[key]}")
        raise InvalidToolArguments(f"{tool.name} has no argument '{key}'")
    for param in tool.params:
        if param.name not in args:
            if param.required:
                raise InvalidToolArguments(f"{tool.name}.{param.name} is required")
            continue
        _check_value(tool, param, args[param.name])

    internal = {
        param.handler_arg: args[param.name]
        for param in tool.params
        if param.handler_arg and param.name in args
    }
    return ResolvedCall(tool.name, tool.handler, internal, True)


def model_visible_names(tools: Iterable[Mapping[str, Any]]) -> list[str]:
    """Names a provider would see for an (already projected) schema list."""
    return [n for n in (_schema_name(t) for t in tools or ()) if n]
