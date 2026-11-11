"""Public workspace tool text must reflect the enforced native sandbox."""
import asyncio
from unittest.mock import patch

from src import containment
from src.agent_tools.filesystem_tools import GetWorkspaceTool


def test_workspace_tool_reports_required_containment_without_claiming_unconfined_shell():
    assert containment.CONTAINMENT_MODE == containment.MODE_ENFORCING

    async def run():
        with patch("src.tool_execution.get_active_workspace", return_value="/tmp/synthetic-project"):
            return await GetWorkspaceTool().execute("", {})

    result = asyncio.run(run())
    assert result["exit_code"] == 0
    assert result["output"].startswith("/workspace\n")
    assert "filesystem/process containment" in result["output"]
    assert "not sandboxed" not in result["output"]
    assert "can reach outside" not in result["output"]

def test_tool_catalog_describes_virtual_workspace_not_host_absolute_directory():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    descriptions = [BUILTIN_TOOL_DESCRIPTIONS["get_workspace"]]
    descriptions.extend(
        item["function"]["description"]
        for item in FUNCTION_TOOL_SCHEMAS
        if item.get("function", {}).get("name") == "get_workspace"
    )
    assert len(descriptions) == 2
    for description in descriptions:
        assert "sandbox-visible /workspace" in description
        assert "not sandboxed" not in description
        assert "can reach outside" not in description


def test_no_workspace_is_not_misrepresented_as_selected_directory():
    async def run():
        with patch("src.tool_execution.get_active_workspace", return_value=""):
            return await GetWorkspaceTool().execute("", {})

    result = asyncio.run(run())
    assert result["exit_code"] == 0
    assert "No workspace is set" in result["output"]
