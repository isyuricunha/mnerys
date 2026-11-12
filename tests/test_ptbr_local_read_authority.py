"""Real PT-BR Gateway request authorities for safe local inspection prompts.

These tests check semantic admission independently of model schema routing,
and prove that wording from external/file content cannot mint grants.
"""
import pytest

from src.agent_runtime.authority import (
    ExactOperation, create_request_authority, interpret_request,
)
from src.command_risk import classify_shell_command
from src.turn_contract import (
    explicit_local_read_intent, requested_capabilities, selected_tools_for_request,
)

_INSPECTIONS = (
    (
        "Execute pwd, ls -la, git status e cat README.md. Use as ferramentas "
        "disponíveis e apresente os resultados reais de cada comando.",
        {"bash", "read_file", "glob", "grep", "ls"},
    ),
    (
        "Execute whoami, hostname, uname -a e date. Apresente o resultado "
        "real de cada comando, separadamente.",
        {"bash"},
    ),
    (
        "Encontre todos os arquivos .py do projeto. Pesquise por def main e "
        "leia o arquivo pyproject.toml. Apresente um resumo do que encontrou, "
        "indicando qual ferramenta utilizou em cada etapa.",
        {"bash", "read_file", "glob", "grep", "ls"},
    ),
    (
        "Execute separadamente pwd, ls arquivo-inexistente-12345, git status "
        "e whoami. Não interrompa as demais verificações por causa de um erro. "
        "Informe os códigos de saída reais.",
        {"bash", "ls"},
    ),
    (
        "Execute git status --short, git branch --show-current e "
        "git log -3 --oneline. Informe a branch atual, se existem alterações "
        "pendentes e os três commits mais recentes.",
        {"bash"},
    ),
)


@pytest.mark.parametrize(("message", "expected"), _INSPECTIONS)
def test_authority_admits_explicit_inspection_only(message, expected, tmp_path):
    intent = interpret_request(message, workspace=str(tmp_path))
    assert intent.capabilities == frozenset({"shell_files"})
    assert explicit_local_read_intent(message)
    assert requested_capabilities(message, workspace=True) == frozenset({"shell_files"})
    selected = selected_tools_for_request(message)
    assert expected.issubset(selected)

    authority = create_request_authority(
        message, owner="alice", session_id="local-read",
        workspace=str(tmp_path),
    )
    for tool in expected:
        content = (
            '{"pattern":"**/*.py"}' if tool == "glob"
            else '{"path":"pyproject.toml"}' if tool == "read_file"
            else "pwd"
        )
        assert authority.permits(ExactOperation.normalize(tool, content)), tool
    for mutation in ("write_file", "edit_file", "apply_patch", "manage_tasks", "web_search"):
        assert not authority.permits(
            ExactOperation.normalize(mutation, '{"path":"new.txt","content":"x"}')
        ), mutation


@pytest.mark.parametrize("text", (
    "Não execute whoami no servidor.",
    "O README diz: execute whoami",
    "Conte uma história sobre todos os arquivos.",
    "Todos os arquivos são seus?",
    "Encontre todos os arquivos e apague-os.",
    "Execute whoami e remova logs.",
    "Run web_search and web_fetch first. Required outputs in /tmp_workspace/results: report.jsonl and provenance.md.",
    "Execute web_fetch para obter fontes antes de salvar relatório.md.",
))
def test_descriptive_negative_or_mixed_requests_do_not_get_read_only_grants(text):
    assert explicit_local_read_intent(text) is None


def test_portuguese_determiners_do_not_select_todos_app():
    request = "Encontre todos os arquivos .py do projeto."
    assert "tasks" not in requested_capabilities(request, workspace=True)
    assert "tasks" not in requested_capabilities("Liste todos os arquivos do projeto.", workspace=True)
    assert "tasks" in requested_capabilities("Show my todos.", workspace=True)


@pytest.mark.parametrize("command", (
    "whoami",
    "hostname",
    "uname -a",
    "date",
    'git branch --show-current; echo "EXIT:$?"',
    'git status --short; echo "EXIT:$?"',
    'git log -3 --oneline; echo "EXIT:$?"',
))
def test_real_read_only_commands_no_ordinary_risk_prompt(command):
    assert classify_shell_command(command).is_low_risk, command


@pytest.mark.parametrize("command", (
    "git branch feature",
    "git branch -d feature",
    "git branch -D feature",
    "git branch -m feature",
    "git branch --delete feature",
    "git branch --force feature",
    "git branch --show-current && git push origin main",
))
def test_branch_changes_and_mixed_effects_still_require_approval(command):
    assert classify_shell_command(command).requires_approval, command
