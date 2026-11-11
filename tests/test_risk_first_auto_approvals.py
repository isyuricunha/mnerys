"""Regression: low-risk reads are unattended; mutations ask unless YOLO.

This covers only the discretionary confirmation layer. Tool policy, user
authority, server-owned YOLO, resource confinement, and non-delegable denial
remain separate mandatory admission checks.
"""
import pytest

from src.command_risk import classify_shell_command
from src.tool_capabilities import ToolRunSecurityContext


@pytest.mark.parametrize("command", [
    "ls -la", "pwd", "cat notes.md", "git status", "git diff",
    "find . -name '*.py'", "grep -n needle notes.md",
])
@pytest.mark.parametrize("external", [False, True])
def test_safe_bash_reads_never_require_confirmation(command, external):
    context = ToolRunSecurityContext(
        external_untrusted_context_seen=external,
        external_sources=["web_search"] if external else [],
    )
    assert classify_shell_command(command).is_low_risk
    assert context.decision_for("bash", command).allowed


@pytest.mark.parametrize("tool,argument", [
    ("ls", '{"path":"."}'),
    ("glob", '{"pattern":"*"}'),
    ("grep", '{"pattern":"needle"}'),
    ("read_file", "notes.md"),
    ("web_search", '{"query":"example"}'),
    ("list_models", "{}"),
])
def test_known_read_only_tools_do_not_prompt_after_external_result(tool, argument):
    context = ToolRunSecurityContext(
        external_untrusted_context_seen=True, external_sources=["web_search"],
    )
    assert context.decision_for(tool, argument).allowed


@pytest.mark.parametrize("command", [
    "rm -rf build", "touch new.txt", "mkdir output", "ln -s source dest",
    "tee output.txt", "git add .", "git commit -m test",
    "git pull", "docker compose up -d",
    "echo data > output.txt", "ls -la; rm -rf build",
])
def test_mutating_shell_is_not_read_only(command):
    assert classify_shell_command(command).requires_approval, command
    normal = ToolRunSecurityContext()
    assert not normal.decision_for("bash", command).allowed
    yolo = ToolRunSecurityContext(
        yolo_enabled=True, external_untrusted_context_seen=True,
        external_sources=["web_search"],
    )
    assert yolo.decision_for("bash", command).allowed


@pytest.mark.parametrize("tool,content", [
    ("write_file", "created.txt\nvalue"),
    ("edit_file", "file.txt\nold\nnew"),
    ("update_document", "new content"),
])
def test_native_mutations_prompt_normal_but_auto_in_yolo(tool, content):
    normal = ToolRunSecurityContext(external_untrusted_context_seen=False)
    assert not normal.decision_for(tool, content).allowed
    yolo = ToolRunSecurityContext(
        yolo_enabled=True, external_untrusted_context_seen=True,
        external_sources=["web_search"],
    )
    assert yolo.decision_for(tool, content).allowed


@pytest.mark.parametrize("command", [
    "cat ~/.ssh/id_rsa", "bash -c 'rm -rf build'",
    "find . -exec rm {} \\;", "echo $(id)",
])
def test_sensitive_or_dynamic_bash_needs_review_in_normal_mode(command):
    ctx = ToolRunSecurityContext(external_untrusted_context_seen=True)
    assert not ctx.decision_for("bash", command).allowed


def test_python_tool_is_not_auto_approved_from_a_superficial_ast_read():
    normal = ToolRunSecurityContext(external_untrusted_context_seen=True)
    # General Python has too many possible effects to treat harmless-looking
    # syntax as sufficient tool authorization.
    assert not normal.decision_for("python", "print('hello')").allowed
    yolo = ToolRunSecurityContext(yolo_enabled=True, external_untrusted_context_seen=True)
    assert yolo.decision_for("python", "print('hello')").allowed


def test_missing_shell_command_does_not_inherit_approval():
    normal = ToolRunSecurityContext(external_untrusted_context_seen=True)
    assert not normal.decision_for("bash", None).allowed
    assert not normal.decision_for("bash", "").allowed


def test_yolo_does_not_lift_delegated_credential_boundary():
    delegated = ToolRunSecurityContext(
        yolo_enabled=True, delegated_credential=True,
        external_untrusted_context_seen=True,
    )
    assert not delegated.decision_for("read_file", "notes.md").allowed


def test_unknown_tools_need_review_even_without_external_content():
    normal = ToolRunSecurityContext()
    decision = normal.decision_for("unregistered_unknown_tool", "{}")
    assert not decision.allowed
    assert "unknown" in decision.reason.lower()


def test_unknown_tools_remain_conservative_after_external_content_even_yolo():
    ctx = ToolRunSecurityContext(
        yolo_enabled=True,
        external_untrusted_context_seen=True,
        external_sources=["web_search"],
    )
    assert not ctx.decision_for("unregistered_unknown_tool", "{}").allowed
