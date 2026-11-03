"""The fixture capability exception cannot grant a denied tool."""
import ast
from pathlib import Path

import pytest


def test_explicit_fixture_selection_respects_all_denials():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/gateway_tool_intent.py").read_text())
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "fixture_personal" for t in node.targets))
    expression = compile(ast.Expression(assignment.value), "fixture-selection", "eval")
    families = {"manage_calendar": "calendar", "manage_notes": "notes", "manage_tasks": "tasks"}
    for disabled, blocked, expected in [
        (set(), set(), {"manage_calendar"}),
        ({"manage_calendar"}, set(), set()),
        (set(), {"manage_calendar"}, set()),
    ]:
        assert eval(expression, {
            "selected": {"manage_calendar"},
            "disabled": disabled,
            "owner_blocked": blocked,
            "_FIXTURE_PERSONAL_TOOL_FAMILIES": families,
        }) == expected
    source = ast.unparse(tree)
    assert 'owner_blocked.difference_update(fixture_personal)' not in source
    assert 'disabled.difference_update(fixture_personal)' not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "guide_only"])
async def test_bound_contract_cannot_override_execution_restrictions(monkeypatch, gate):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src import tool_execution, tool_implementations
    from src.tool_policy import build_effective_tool_policy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import bind_turn_contract, resolve_turn_contract

    handler = AsyncMock(return_value={"exit_code": 0})
    monkeypatch.setattr(tool_implementations, "do_manage_calendar", handler)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    contract = resolve_turn_contract(capabilities={"calendar"}, schemas=FUNCTION_TOOL_SCHEMAS,
                                     policy=build_effective_tool_policy())
    assert contract.permits("manage_calendar")
    with bind_turn_contract(contract):
        desc, result = await tool_execution.execute_tool_block(
            SimpleNamespace(tool_type="manage_calendar", content='{"action":"list"}'),
            disabled_tools={"manage_calendar"} if gate == "disabled" else set(),
            tool_policy=build_effective_tool_policy(last_user_message="Do not use tools.") if gate == "guide_only" else None,
            security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
        )
    assert desc == "manage_calendar: BLOCKED"
    assert result["exit_code"] == 1
    handler.assert_not_awaited()
