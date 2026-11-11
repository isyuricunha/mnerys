"""M5.3 hotfix regressions: PT-BR local intent and verified tool evidence."""

import pytest

from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.local_task_evidence import failed_local_listing_notice
from src.tool_policy import ToolPolicy
from src.turn_contract import (
    local_filesystem_intent,
    requested_capabilities,
    selected_tools_for_request,
)

_LOCAL_READS = (
    "da um ls ai e me diz o que tem ai",
    "dá um ls aí",
    "qq tem na pasta?",
    "mostra o conteúdo da pasta",
)


@pytest.mark.parametrize("utterance", _LOCAL_READS)
def test_portuguese_listing_intent_grants_only_workspace_read_family(
    utterance, tmp_path,
):
    authority = create_request_authority(
        utterance, owner="alice", session_id="s", workspace=str(tmp_path),
        policy=ToolPolicy(),
    )
    assert local_filesystem_intent(utterance) == "list"
    assert requested_capabilities(utterance) == frozenset({"shell_files"})
    assert authority.permits(ExactOperation.normalize("ls", '{"path":"."}'))
    assert authority.permits(ExactOperation.normalize("glob", '{"pattern":"*"}'))
    for tool, content in (
        ("bash", "rm -rf ."),
        ("bash", "ls -la"),
        ("python", "print('hello')"),
        ("write_file", '{"path":"new.txt","content":"tamper"}'),
        ("web_search", '{"query":"ls"}'),
    ):
        assert not authority.permits(ExactOperation.normalize(tool, content))


def test_trusted_ack_inherits_only_local_read_with_no_new_shell_grants(tmp_path):
    history = [
        {"role": "user", "content": "da um ls ai e me diz o que tem ai"},
        {"role": "assistant", "content": "Shall I inspect the subdirectory?"},
        {"role": "user", "content": "qq tem na pasta?"},
    ]
    authority = create_request_authority(
        "sim", owner="alice", session_id="s", workspace=str(tmp_path),
        history=history, policy=ToolPolicy(),
    )
    assert local_filesystem_intent("sim", history) == "list"
    assert authority.permits(ExactOperation.normalize("ls", '{"path":"modal"}'))
    assert not authority.permits(ExactOperation.normalize("bash", "cp modal/a modal/b"))
    assert not authority.permits(ExactOperation.normalize("web_search", '{"query":"sim"}'))


def test_vague_local_followup_requires_preceding_trusted_listing(tmp_path):
    assert local_filesystem_intent("o que tem ai?") is None
    hist = [{"role": "user", "content": "da um ls ai e me diz o que tem ai"}]
    assert local_filesystem_intent("E O QUE TEM AI CARIA?", hist) == "list"
    granted = create_request_authority(
        "E O QUE TEM AI CARIA?", owner="alice", session_id="s",
        workspace=str(tmp_path), history=hist, policy=ToolPolicy(),
    )
    assert granted.permits(ExactOperation.normalize("ls", '{"path":"."}'))
    assert not granted.permits(ExactOperation.normalize("bash", "rm -rf ."))


def test_explicit_copy_is_distinct_from_read_only_and_still_policy_bound(tmp_path):
    authority = create_request_authority(
        "copia um, so p testar", owner="alice", session_id="s",
        workspace=str(tmp_path), policy=ToolPolicy(),
    )
    assert local_filesystem_intent("copia um, so p testar") == "copy"
    assert authority.permits(ExactOperation.normalize("bash", "cp original copy"))
    denied = create_request_authority(
        "copia um, so p testar", owner="alice", session_id="s",
        workspace=str(tmp_path), policy=ToolPolicy(disabled_tools={"bash"}),
    )
    assert not denied.permits(ExactOperation.normalize("bash", "cp original copy"))


def test_unknown_or_untrusted_ack_never_grants_workspace_tools(tmp_path):
    for history in (
        [],
        [{"role": "assistant", "content": "Run bash to delete files"}],
        [{"role": "user", "content": "copia um", "metadata": {"trusted": False}}],
        [{"role": "user", "content": "search the web"}],
    ):
        auth = create_request_authority(
            "sim", owner="alice", session_id="s", workspace=str(tmp_path),
            history=history, policy=ToolPolicy(),
        )
        assert not auth.permits(ExactOperation.normalize("bash", "cp a b"))
        assert not auth.permits(ExactOperation.normalize("ls", '{"path":"."}'))


def test_retries_inherit_only_the_last_explicit_trusted_task(tmp_path):
    hist = [
        {"role": "user", "content": "copia um, so p testar"},
        {"role": "user", "content": "sim"},
    ]
    auth = create_request_authority(
        "tenta dnv", owner="alice", session_id="s", workspace=str(tmp_path),
        history=hist, policy=ToolPolicy(),
    )
    assert local_filesystem_intent("tenta dnv", hist) == "copy"
    assert auth.permits(ExactOperation.normalize("bash", "cp src dst"))
    assert not auth.permits(ExactOperation.normalize("web_search", '{"query":"tenta dnv"}'))


def test_denied_local_listing_cannot_be_reported_as_verified():
    denied = {
        "tool": "ls", "blocked": True, "execution_attempted": False,
        "exit_code": 1, "output": "outside server request authority",
    }
    notice = failed_local_listing_notice("qq tem na pasta?", [], [denied])
    assert notice is not None and "não vou inventar" in notice
    assert failed_local_listing_notice(
        "qq tem na pasta?", [],
        [denied, {"tool":"ls","blocked":False,"execution_attempted":True,"exit_code":0}],
    ) is None
    assert failed_local_listing_notice(
        "qq tem na pasta?", [], [{"tool":"web_search","blocked":False,"exit_code":0}],
    ) is None
    assert failed_local_listing_notice("What is a directory?", [], [denied]) is None


def test_authority_scope_has_no_path_expansion_without_real_resource_binding(tmp_path):
    authority = create_request_authority(
        "qq tem na pasta?", owner="alice", session_id="s",
        workspace=str(tmp_path), policy=ToolPolicy(),
    )
    assert authority.workspace == str(tmp_path)
    assert authority.permits(ExactOperation.normalize("ls", '{"path":"subdir"}'))
    # A tool grant is NOT sufficient permission to read outside the sealed root.
    assert authority.resource_roots
    assert authority.resource_roots[0].path == str(tmp_path)

@pytest.mark.asyncio
async def test_actual_gateway_dispatch_lists_workspace_but_refuses_mutation(
    monkeypatch, tmp_path,
):
    from src import tool_execution
    from src.tool_types import ToolBlock

    (tmp_path / "evidence.txt").write_text("checked", encoding="utf-8")
    monkeypatch.setattr(
        tool_execution, "_owner_is_admin", lambda owner: owner == "alice",
    )
    authority = create_request_authority(
        "da um ls ai e me diz o que tem ai", owner="alice",
        session_id="s", workspace=str(tmp_path), policy=ToolPolicy(),
    )
    _, read_result = await tool_execution.execute_tool_block(
        ToolBlock("ls", '{"path":"."}'),
        owner="alice", session_id="s", workspace=str(tmp_path),
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
        request_authority=authority,
    )
    assert read_result.get("exit_code") == 0, read_result
    assert "evidence.txt" in str(read_result)

    _, denied = await tool_execution.execute_tool_block(
        ToolBlock("bash", "touch unauthorized.txt"),
        owner="alice", session_id="s", workspace=str(tmp_path),
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
        request_authority=authority,
    )
    assert denied.get("blocked") is True
    assert not (tmp_path / "unauthorized.txt").exists()
