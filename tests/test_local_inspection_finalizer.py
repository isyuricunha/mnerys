"""Terminal text fallback only when model ignores already completed local reads."""
from src.agent_runtime.tool_output_presentation import (
    summarize_incomplete_local_inspection,
    strip_completed_inspection_preamble,
)

REQUEST = (
    "Execute whoami, hostname, uname -a e date. "
    "Apresente o resultado real de cada comando, separadamente."
)
READS = [
    {"tool": "bash", "command": "whoami", "output": "ubuntu", "exit_code": 0},
    {"tool": "bash", "command": "hostname", "output": "example-host", "exit_code": 0},
    {"tool": "bash", "command": "uname -a", "output": "Linux example", "exit_code": 0},
    {"tool": "bash", "command": "date", "output": "Thu Nov 12", "exit_code": 0},
]


def test_completed_inspection_strips_leaked_plan_without_touching_summary():
    visible = (
        "I'll work through each step in parallel where possible."
        "Segue o resumo, indicando a ferramenta usada em cada etapa: "
        "Glob encontrou arquivos Python; Grep localizou os pontos de entrada; "
        "Read examinou o pyproject.toml."
    )
    message = (
        "Encontre todos os arquivos .py do projeto. Pesquise por def main e "
        "leia o arquivo pyproject.toml. Apresente um resumo do que encontrou."
    )
    cleaned = strip_completed_inspection_preamble(
        visible, READS, user_request=message,
    )
    assert cleaned.startswith("Segue o resumo")
    assert "Glob encontrou" in cleaned
    assert "I'll work" not in cleaned


def test_preamble_is_not_removed_without_verified_read_or_answer():
    message = "Execute whoami, hostname, uname -a e date. Apresente resultados reais."
    plan = "I'll work through each step in parallel where possible."
    assert strip_completed_inspection_preamble(
        plan, READS, user_request=message,
    ) == plan
    answer = plan + " O resultado não está disponível."
    assert strip_completed_inspection_preamble(
        answer, [], user_request=message,
    ) == answer
    assert strip_completed_inspection_preamble(
        answer, [dict(tool="bash", command="rm x", output="", exit_code=0)],
        user_request=message,
    ) == answer


def test_inspection_preamble_is_not_removed_after_blocked_or_effectful_call():
    message = "Execute whoami, hostname, uname -a e date. Apresente resultados reais."
    answer = (
        "I'll work through each step in parallel where possible."
        "Os resultados observados mostram os quatro comandos e respectivos códigos."
    )
    for extra in (
        {"tool": "bash", "command": "mkdir reports", "output": "", "exit_code": 0},
        {"tool": "bash", "command": "hostname", "output": "", "exit_code": 1, "blocked": True},
        {"tool": "unknown_tool", "output": "x", "exit_code": 0},
    ):
        assert strip_completed_inspection_preamble(
            answer, READS + [extra], user_request=message,
        ) == answer


def test_complete_git_log_is_reported_when_three_lines_fit():
    message = (
        "Execute git status --short, git branch --show-current e "
        "git log -3 --oneline. Informe a branch e os três commits mais recentes."
    )
    commits = (
        "1a2b3c4 feat(example): introduce a deterministic multi-step code inspection flow\\n"
        "2b3c4d5 fix(example): enforce consistent completion evidence across platform clients\\n"
        "3c4d5e6 docs(example): document reliable and isolated workspace command reporting"
    )
    assert len(commits) > 220
    assert commits.count("\\n") == 2
    events = [
        {"tool": "bash", "command": "git status --short", "output": "", "exit_code": 0},
        {"tool": "bash", "command": "git branch --show-current", "output": "master", "exit_code": 0},
        {"tool": "bash", "command": "git log -3 --oneline", "output": commits, "exit_code": 0},
    ]
    result = summarize_incomplete_local_inspection(
        "", events, user_request=message,
    )
    assert result is not None
    assert "1a2b3c4" in result and "2b3c4d5" in result and "3c4d5e6" in result
    assert "Saída completa disponível" not in result
    assert "    1a2b3c4" in result


def test_last_stdout_echo_is_replaced_by_actual_multi_command_receipts():
    answer = summarize_incomplete_local_inspection(
        "ubuntu", READS, user_request=REQUEST,
    )
    assert answer is not None
    for command in ("whoami", "hostname", "uname -a", "date"):
        assert command in answer
    assert answer.count("exit `0`") == 4
    assert "example-host" in answer
    assert "Thu Nov 12" in answer


def test_plaintext_fence_containing_last_command_output_is_incomplete():
    answer = summarize_incomplete_local_inspection(
        "```text\nubuntu\n```", READS, user_request=REQUEST,
    )
    assert answer is not None
    assert answer.count("exit `0`") == 4


def test_redundant_repeat_does_not_overwrite_successful_prior_evidence():
    events = READS + [{
        "tool": "bash", "command": "whoami",
        "error": "Blocked an exact repeat of a successful read-only call from round 1",
        "exit_code": 2, "blocked": True, "policy": "repeated_read_only_call",
    }]
    answer = summarize_incomplete_local_inspection(
        "Blocked an exact repeat of a successful read-only call from round 1",
        events, user_request=REQUEST,
    )
    assert answer is not None
    assert answer.count("exit `0`") == 4
    assert "exit `2`" not in answer
    assert "example-host" in answer


def test_missing_receipt_prevents_optimistic_completion():
    events = READS + [{
        "tool": "bash", "command": "cat README.md",
        "error": "The exact operation is outside server request authority.",
        "exit_code": 1, "blocked": True,
    }]
    assert summarize_incomplete_local_inspection(
        "ubuntu", events, user_request=REQUEST,
    ) is None


def test_real_command_error_remains_failure_and_next_commands_are_reported():
    events = [
        {"tool": "bash", "command": "pwd", "output": "/workspace", "exit_code": 0},
        {"tool": "bash", "command": "ls arquivo-inexistente-12345",
         "output": "No such file or directory", "exit_code": 2},
        {"tool": "bash", "command": "git status",
         "output": "On branch master", "exit_code": 0},
        {"tool": "bash", "command": "whoami", "output": "ubuntu", "exit_code": 0},
    ]
    request = (
        "Execute separadamente pwd, ls arquivo-inexistente-12345, "
        "git status e whoami. Não interrompa as demais verificações "
        "por causa de um erro. Informe os códigos de saída reais."
    )
    answer = summarize_incomplete_local_inspection(
        "ubuntu", events, user_request=request,
    )
    assert "exit `2`" in answer
    assert "No such file or directory" in answer
    assert "git status" in answer
    assert "whoami" in answer


def test_actual_complete_answer_never_replaced():
    assert summarize_incomplete_local_inspection(
        "Results: whoami ubuntu; hostname example-host; uname Linux; date Nov 12.",
        READS, user_request=REQUEST,
    ) is None


def test_single_event_never_claims_multi_command_completion():
    assert summarize_incomplete_local_inspection(
        "ubuntu", READS[:1], user_request=REQUEST,
    ) is None


def test_mutating_code_does_not_gain_a_false_completion():
    events = READS + [{
        "tool": "bash", "command": "mkdir -p output", "output": "",
        "exit_code": 0,
    }]
    assert summarize_incomplete_local_inspection(
        "ubuntu", events, user_request=REQUEST,
    ) is None


def test_unknown_tool_and_approval_do_not_generate_fake_receipts():
    for extra in (
        {"tool": "future_tool", "command": "x", "exit_code": 0},
        {"tool": "bash", "command": "whoami", "approval_required": True, "exit_code": None},
    ):
        assert summarize_incomplete_local_inspection(
            "ubuntu", READS + [extra], user_request=REQUEST,
        ) is None


def test_not_for_other_questions_or_email_data():
    assert summarize_incomplete_local_inspection(
        "ubuntu", READS, user_request="What is the best Linux distro?",
    ) is None
    assert summarize_incomplete_local_inspection(
        "ubuntu", READS, user_request="Apenas diga whoami.",
    ) is None


def test_full_long_output_is_not_copied_into_final_answer():
    readme = "\n".join("line " + str(i) for i in range(200))
    events = READS + [{
        "tool": "read_file", "command": "README.md",
        "output": readme, "exit_code": 0,
    }]
    answer = summarize_incomplete_local_inspection(
        "ubuntu", events,
        user_request=(
            "Execute whoami e leia o README.md. Apresente o resultado real "
            "de cada comando separadamente."
        ),
    )
    assert answer is not None
    assert "Saída completa disponível" in answer
    assert "line 150" not in answer
