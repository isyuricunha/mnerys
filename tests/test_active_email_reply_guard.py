"""Contract tests for the active-email reader's immediate-action restrictions.

Inspect the Python guard itself rather than using nearby comments as a boundary.
This protects the email review workflow even when route policy orchestration is
moved into the Gateway.
"""

import ast
from pathlib import Path

import pytest


ROUTE_PATH = Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
RESTRICTED_TOOLS = {
    "create_document",
    "send_email",
    "reply_to_email",
    "mcp__email__send_email",
    "mcp__email__reply_to_email",
}


def _active_email_guard() -> ast.If:
    tree = ast.parse(ROUTE_PATH.read_text(encoding="utf-8"))
    expected = ast.dump(
        ast.parse(
            'active_email_ctx and active_email_ctx.get("uid")', mode="eval"
        ).body,
        include_attributes=False,
    )
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.dump(node.test, include_attributes=False) == expected
    ]
    assert len(guards) == 1, "Active-email guard must appear once in chat routes"
    return guards[0]


def test_active_email_reader_blocks_immediate_reply_tools():
    guard = _active_email_guard()
    updates = [
        node.value
        for node in guard.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "disabled_tools"
        and node.value.func.attr == "update"
    ]
    assert len(updates) == 1, "Email reader must update the restricted tools"
    assert len(updates[0].args) == 1
    assert RESTRICTED_TOOLS <= set(ast.literal_eval(updates[0].args[0]))


@pytest.mark.parametrize(
    ("active_email_ctx", "should_block"),
    [
        ({"uid": "message-1"}, True),
        ({"uid": ""}, False),
        ({}, False),
        (None, False),
    ],
)
def test_email_restrictions_only_activate_for_an_open_email(
    active_email_ctx, should_block
):
    """Execute the real guarded statement with isolated local state."""
    guard = _active_email_guard()
    statement = ast.fix_missing_locations(
        ast.Module(body=[guard], type_ignores=[])
    )
    namespace = {
        "active_email_ctx": active_email_ctx,
        "disabled_tools": set(),
    }
    exec(compile(statement, str(ROUTE_PATH), "exec"), {"__builtins__": {}}, namespace)
    assert (RESTRICTED_TOOLS <= namespace["disabled_tools"]) is should_block
