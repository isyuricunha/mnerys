"""M5.2 increment 5: ToolSearch and Skill — real discovery and activation.

Both capabilities are provider-independent canonical tools that run through
the single existing Gateway path. They surface what already exists and is
already authorized; they never grant authority.

``ToolSearch`` (handler ``tool_search``)
    Ranks the turn's policy-admitted tool inventory — the snapshot frozen in
    the bound :class:`~src.turn_contract.TurnContract` — by case-insensitive
    Python regex over tool names, descriptions, argument names and argument
    descriptions (the same search space Anthropic's Tool Search documents).
    Discovered tools that are not yet offered are activated server-side via
    :func:`~src.turn_contract.activate_discovered_tools`, which widens only
    the model-visible offer inside the already-admitted executable set.
    Policy-hidden, disabled and unknown tools are absent from the snapshot
    and cannot leak their schemas. MCP tools keep their original
    ``mcp__<server>__<tool>`` identities.

``Skill`` (handler ``activate_skill``)
    Activates one installed skill from the existing SKILL.md library
    (``services.memory.skills.SkillsManager``) with the same owner scoping
    and approval/audit eligibility the system prompt's skill index applies,
    or returns the compact eligible index when called without a name.
    Skill content is untrusted instructions to apply through normal
    authorized tools: activating a skill never changes tool policy, never
    grants permissions, and never executes anything.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from src.turn_contract import active_turn_contract, activate_discovered_tools

logger = logging.getLogger(__name__)

# Official Tool Search bounds: patterns are Python re.search() patterns of at
# most 200 characters; discovery returns up to 5 tools unless the model raises
# the limit (hard cap 20 to keep results bounded and cache-friendly).
_MAX_PATTERN_CHARS = 200
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20

# Skill activation bounds: SKILL.md bodies and reference files are
# instructions, not bulk data — keep loaded content bounded.
_MAX_SKILL_CHARS = 32_000
_MAX_REFERENCE_CHARS = 16_000
_MAX_SKILL_FILE_BYTES = 1_000_000
_MAX_INDEX_ENTRIES = 50

_BUILTIN_HANDLERS: frozenset[str] = frozenset()


def _builtin_handlers() -> frozenset[str]:
    """Registry handler ids (lazily: keeps this module import-light)."""
    global _BUILTIN_HANDLERS
    if not _BUILTIN_HANDLERS:
        try:
            from src.tool_registry import HANDLER_IDS

            _BUILTIN_HANDLERS = HANDLER_IDS
        except Exception:  # pragma: no cover - registry is a leaf import
            _BUILTIN_HANDLERS = frozenset()
    return _BUILTIN_HANDLERS


def _wire_name(inventory_name: str) -> str:
    """Model-visible name for an inventory schema (canonical projection)."""
    try:
        from src.tool_registry import canonical_name_for_handler

        return canonical_name_for_handler(inventory_name) or inventory_name
    except Exception:  # pragma: no cover
        return inventory_name


def _source_of(inventory_name: str) -> str:
    if inventory_name.startswith("mcp__"):
        parts = inventory_name.split("__")
        return f"mcp:{parts[1]}" if len(parts) > 2 else "mcp"
    return "builtin" if inventory_name in _builtin_handlers() else "inherited"


def _searchable_text(name: str, schema: dict) -> str:
    """The official search space: name, description, argument names/descriptions."""
    function = schema.get("function") or {}
    parts = [name, str(function.get("description") or "")]
    parameters = function.get("parameters") or {}
    for arg_name, param in (parameters.get("properties") or {}).items():
        parts.append(str(arg_name))
        if isinstance(param, dict):
            parts.append(str(param.get("description") or ""))
    return "\n".join(parts)


def _rank(entry: dict) -> tuple:
    """Deterministic ranking: name hits first, then shorter names, then order."""
    return (entry["_match_rank"], len(entry["name"]), entry["name"])


def _parse_strict_args(content: Any, tool: str) -> dict:
    """Parse handler arguments defensively for non-registry callers."""
    raw = (content or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"{tool}: arguments are not valid JSON")
    if not isinstance(parsed, dict):
        raise ValueError(f"{tool}: arguments must be a JSON object")
    return parsed


class ToolSearchTool:
    async def execute(self, content, ctx):
        try:
            args = _parse_strict_args(content, "ToolSearch")
        except ValueError as exc:
            return f"ToolSearch: invalid", {"error": str(exc), "exit_code": 1}

        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return "ToolSearch: invalid", {
                "error": "ToolSearch.pattern is required and must be a non-empty string",
                "exit_code": 1,
            }
        pattern = pattern[:_MAX_PATTERN_CHARS]
        limit = args.get("limit", _DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int):
            return "ToolSearch: invalid", {
                "error": "ToolSearch.limit must be an integer",
                "exit_code": 1,
            }
        limit = max(1, min(_MAX_LIMIT, limit))
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return "ToolSearch: invalid", {
                "error": f"ToolSearch.pattern is not a valid Python regular expression: {exc}",
                "exit_code": 1,
            }

        contract = active_turn_contract()
        if contract is not None:
            schemas = contract.discoverable_schemas()
            provenance = {
                "inventory": "admitted turn inventory",
                "offered": len(contract.offered),
            }
        else:
            # No bound turn contract (plain preview/recovery paths): search the
            # registry's real built-ins — public schemas only, no MCP and no
            # inherited surface, because no policy context exists to filter it.
            try:
                from src.tool_registry import REGISTRY

                schemas = [tool.function_schema() for tool in REGISTRY.values()]
            except Exception:
                logger.exception("ToolSearch registry fallback failed")
                schemas = []
            provenance = {"inventory": "canonical registry"}

        matches: list[dict] = []
        for schema in schemas:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if not name:
                continue
            function = schema.get("function") or {}
            properties = (function.get("parameters") or {}).get("properties") or {}
            haystack = _searchable_text(name, schema)
            if not regex.search(haystack):
                continue
            description = str(function.get("description") or "")
            # Rank: a tool-name hit is strongest, then an argument-name hit,
            # then a description-only hit — matching how specific the evidence
            # is. Ties break deterministically in `_rank`.
            if regex.search(name):
                rank = 0
            elif any(regex.search(str(arg_name)) for arg_name in properties):
                rank = 1
            else:
                rank = 2
            matches.append({
                "name": _wire_name(name),
                "inventory_name": name,
                "description": description,
                "source": _source_of(name),
                "schema": schema,
                "_match_rank": rank,
            })
        matches.sort(key=_rank)
        total = len(matches)
        results = matches[:limit]

        # Activation: everything this turn may execute but does not yet offer
        # becomes invocable through the server-side contract bridge. Refused
        # names are reported truthfully; they never reach the executable set.
        activation = activate_discovered_tools(
            [entry["inventory_name"] for entry in results]
        ) if contract is not None else {"activated": [], "already_offered": [], "refused": []}

        for entry in results:
            entry.pop("_match_rank", None)
            schema = entry.pop("schema", None)
            if schema is not None:
                entry["parameters"] = (schema.get("function") or {}).get("parameters") or {}

        labels = ", ".join(entry["name"] for entry in results) or "none"
        # The model-facing text must carry the actual discovery details —
        # names, descriptions and argument schemas — because the feedback
        # path renders `output` for both native and textual channels. Bounded
        # per entry to stay cache-friendly.
        sections = []
        for entry in results:
            section = f"- {entry['name']} [{entry['source']}]: {entry['description']}"
            parameters = entry.get("parameters")
            if parameters:
                rendered = json.dumps(parameters, ensure_ascii=False)
                if len(rendered) > 1200:
                    rendered = rendered[:1200] + " …"
                section += f"\n  arguments: {rendered}"
            sections.append(section)
        return f"ToolSearch: {pattern[:80]} -> {len(results)} tool(s)", {
            "tools": results,
            "total_matches": total,
            "activated": activation.get("activated", []),
            "activation_refused": activation.get("refused", []),
            "provenance": provenance,
            "output": (
                f"{total} tool(s) match {pattern!r}; returning {len(results)}:\n"
                + ("\n".join(sections) if sections else "No matching tools.")
                + (
                    "\nNewly invocable this turn: " + ", ".join(activation.get("activated", [])) + "."
                    if activation.get("activated") else ""
                )
                + "\nDiscovery does not grant permission: denied tools are not searchable, "
                "and each invocation still passes admission and approval."
            ),
            "exit_code": 0,
        }


class SkillTool:
    async def execute(self, content, ctx):
        try:
            args = _parse_strict_args(content, "Skill")
        except ValueError as exc:
            return "Skill: invalid", {"error": str(exc), "exit_code": 1}

        name = args.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return "Skill: invalid", {
                "error": "Skill.name must be a non-empty string when provided",
                "exit_code": 1,
            }
        name = str(name or "").strip()[:80]
        path = args.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            return "Skill: invalid", {
                "error": "Skill.path must be a non-empty string when provided",
                "exit_code": 1,
            }
        path = str(path or "").strip()[:200]
        owner = (ctx or {}).get("owner") if isinstance(ctx, dict) else None

        from services.memory.skills import SkillsManager, activation_eligible
        from services.memory.skill_format import Skill
        from src.constants import DATA_DIR

        sm = SkillsManager(DATA_DIR)

        if not name:
            # Compact, cache-friendly discovery metadata — the same eligible
            # index the system prompt carries.
            try:
                eligible = sm.index_for(owner=owner)
            except Exception:
                eligible = []
            eligible = eligible[:_MAX_INDEX_ENTRIES]
            listing = "\n".join(
                f"- {s.get('name')} [{s.get('category') or 'general'}]: {s.get('description')}"
                for s in eligible
            ) or "No skills are available."
            return "Skill: index", {
                "skills": eligible,
                "count": len(eligible),
                "output": (
                    f"{len(eligible)} skill(s) available:\n{listing}\n"
                    "Activate one by name to load its instructions."
                ),
                "exit_code": 0,
            }

        all_skills = sm.load_all()
        visible = [s for s in sm.load(owner=owner) if s.get("name") == name]
        if not visible:
            known_anywhere = any(s.get("name") == name for s in all_skills)
            if known_anywhere:
                # Truthful without leaking the foreign skill's metadata.
                return "Skill: denied", {
                    "error": f"Skill '{name}' exists but is not available to this owner",
                    "exit_code": 1,
                }
            return "Skill: not found", {
                "error": f"Skill '{name}' is not installed. Call Skill without arguments to list available skills.",
                "exit_code": 1,
            }
        skill = visible[0]

        prefs = {}
        try:
            from routes.prefs_routes import _load_for_user

            prefs = _load_for_user(owner) or {}
        except Exception:
            prefs = {}
        try:
            from src.settings import get_setting

            default_minimum = float(get_setting("skill_autosave_min_confidence", 0.85))
        except Exception:
            default_minimum = 0.85
        try:
            min_confidence = float(prefs.get("skill_min_confidence", default_minimum))
        except (TypeError, ValueError):
            min_confidence = default_minimum
        auto_approve = bool(prefs.get("auto_approve_skills", True))
        if not activation_eligible(skill, auto_approve=auto_approve, min_confidence=min_confidence):
            status = skill.get("status") or "draft"
            return "Skill: disabled", {
                "error": (
                    f"Skill '{name}' is {status} and has not passed the approval/audit "
                    "gate for activation. Publish and audit it first."
                ),
                "exit_code": 1,
            }

        md = sm.read_skill_md(name, owner=owner)
        if md is None:
            return "Skill: unreadable", {
                "error": f"Skill '{name}' could not be read from storage",
                "exit_code": 1,
            }
        try:
            parsed = Skill.from_markdown(md, path=None)
        except Exception:
            parsed = None

        skill_path = skill.get("path")
        if (
            skill_path
            and os.path.exists(skill_path)
            and os.path.getsize(skill_path) > _MAX_SKILL_FILE_BYTES
        ):
            return "Skill: too large", {
                "error": f"Skill '{name}' exceeds the activation size limit",
                "exit_code": 1,
            }
        instructions = md[:_MAX_SKILL_CHARS]
        truncated = len(md) > _MAX_SKILL_CHARS

        result: dict[str, Any] = {
            "skill": name,
            "description": skill.get("description") or "",
            "category": skill.get("category") or "general",
            "version": skill.get("version") or "",
            "provenance": {
                "storage": "data/skills (SKILL.md)",
                "owner_scope": owner or "shared built-ins",
                "status": skill.get("status") or "published",
            },
            "results": instructions,
            "output": (
                f"Activated skill '{name}'"
                + (" (truncated)" if truncated else "")
                + ". Apply its instructions through your normal authorized tools; "
                "the skill itself grants no permissions."
            ),
            "exit_code": 0,
        }
        if truncated:
            result["truncated"] = True

        if path:
            ref = sm.read_skill_reference(name, path, owner=owner)
            if ref is None:
                return f"Skill: reference unavailable", {
                    "error": (
                        f"Reference '{path}' not found under skill '{name}' (paths are "
                        "confined to the skill's own directory; traversal is refused)."
                    ),
                    "exit_code": 1,
                }
            if skill_path:
                # Bound the underlying file the same way the skill body is
                # bounded, through the SAME confinement the read used — never
                # through a raw join of the untrusted relative path.
                from src.path_confinement import PathEscape, confine

                try:
                    ref_file = confine(os.path.dirname(skill_path), path, allow_root=False)
                    if os.path.exists(ref_file) and os.path.getsize(ref_file) > _MAX_SKILL_FILE_BYTES:
                        return "Skill: too large", {
                            "error": f"Reference '{path}' exceeds the activation size limit",
                            "exit_code": 1,
                        }
                except (PathEscape, ValueError, OSError):
                    return "Skill: reference unavailable", {
                        "error": (
                            f"Reference '{path}' escaped the skill's directory and was refused."
                        ),
                        "exit_code": 1,
                    }
            result["reference"] = {
                "path": path,
                "content": ref[:_MAX_REFERENCE_CHARS],
                "truncated": len(ref) > _MAX_REFERENCE_CHARS,
            }

        try:
            sm.record_use(name, owner=owner)
        except Exception:
            logger.debug("skill use recording failed", exc_info=True)
        if parsed is not None and parsed.name and parsed.name != name:
            # The manifest's own slug wins for identity reporting, but the
            # activation identity stays what the caller asked for.
            result["manifest_name"] = parsed.name
        return f"Skill: {name}", result
