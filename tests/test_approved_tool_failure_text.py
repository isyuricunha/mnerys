"""Approval failures must not claim a command never started when it returned stdout."""
from src.agent_runtime.approved_tool_failure import (
    approved_action_failure_text, can_continue_after_approved_shell_failure,
)


def test_only_observed_read_only_approved_failure_can_continue():
    cmd = 'pwd && echo LS && ls -la && git status 2>&1 && cat README.md 2>&1'
    result = {'output': '/workspace\\nGit error: not a repository', 'exit_code': 128}
    assert can_continue_after_approved_shell_failure('bash', cmd, result)
    assert not can_continue_after_approved_shell_failure('bash', cmd, {**result, 'output': ''})
    assert not can_continue_after_approved_shell_failure('bash', cmd, {**result, 'blocked': True})
    assert not can_continue_after_approved_shell_failure('bash', cmd, {**result, 'error': 'denied'})
    assert not can_continue_after_approved_shell_failure('bash', 'rm -rf build && pwd', result)
    assert not can_continue_after_approved_shell_failure('bash', 'echo x > report.txt && pwd', result)
    assert not can_continue_after_approved_shell_failure('host_shell', cmd, result)


def test_executed_nonzero_shell_is_not_labeled_as_unexecuted():
    actual = approved_action_failure_text(
        "bash", "pwd && git status 2>&1 && cat README.md",
        {"output": "/workspace\\nGit error: not a repository", "exit_code": 128},
    )
    assert "returned exit code 128" in actual
    assert "Observed output:" in actual
    assert "/workspace" in actual
    assert "may not have executed" in actual
    assert "could not run" not in actual
    assert "before execution" not in actual


def test_denial_is_distinguished_from_command_error():
    actual = approved_action_failure_text(
        "bash", "pwd",
        {"error": "No matching authority", "blocked": True, "exit_code": 1,
         "failure_kind": "request_authority_denied"},
    )
    assert "blocked before execution" in actual
    assert "No matching authority" in actual
    assert "Observed output" not in actual


def test_non_shell_failure_uses_reported_error_and_output():
    actual = approved_action_failure_text(
        "read_file", '{"path":"README.md"}',
        {"error": "Not found", "output": "No matching file", "exit_code": 1},
    )
    assert "returned exit code 1" in actual
    assert "Error: Not found" in actual
    assert "Observed output:" in actual
    assert "&&" not in actual


def test_unavailable_shell_runtime_is_not_reported_as_started():
    actual = approved_action_failure_text(
        "bash", "pwd",
        {"error": "Containment unavailable", "failure_kind": "containment_unavailable",
         "exit_code": 1},
    )
    assert "blocked before execution" in actual


def test_failed_approval_summary_is_bounded():
    actual = approved_action_failure_text(
        "bash", "pwd", {"output": "Z" * 20000, "exit_code": 1},
    )
    assert len(actual) <= 4000
    assert actual.endswith("[Output truncated]")
