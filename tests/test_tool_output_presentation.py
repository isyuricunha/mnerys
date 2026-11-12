"""Provider-neutral regression for post-tool answers and raw output presentation."""
from src.agent_loop import _visible_response_text
from src.agent_runtime.tool_output_presentation import summarize_repeated_tool_output
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks


def sample_readme():
    intro = "# Example integration\n\n## Usage\n\n"
    commands = "```bash\nuv sync --all-groups\npython scripts/probe.py\n```"
    rest = "\n\n## Technical reference\n" + "\n".join(
        f"- documented interface {index}: supported by the source implementation and tested."
        for index in range(38)
    )
    return intro + commands + rest


def receipts(readme):
    return [
        {"tool": "bash", "command": "pwd", "output": "/workspace", "exit_code": 0},
        {"tool": "bash", "command": "ls -la", "output": "README.md\n.git", "exit_code": 0},
        {"tool": "bash", "command": "git status", "output": "On branch main\nclean", "exit_code": 0},
        {"tool": "bash", "command": "cat README.md", "output": readme, "exit_code": 0},
    ]


def test_native_markdown_fence_is_data_not_a_shell_call():
    document = sample_readme()
    assert parse_tool_blocks(document, skip_fenced=True) == []
    assert strip_tool_blocks(document, skip_fenced=True) == document.strip()
    assert _visible_response_text(document, preserve_code_fences=True) == document.strip()
    assert "uv sync --all-groups" in _visible_response_text(document, preserve_code_fences=True)


def test_web_history_save_preserves_native_fenced_code():
    from routes.chat_routes import _visible_response_text_for_save

    text = sample_readme()
    assert "uv sync --all-groups" in _visible_response_text_for_save(text)
    assert "```bash" in _visible_response_text_for_save(text)


def test_compatibility_textual_tool_fence_still_strips_executed_calls():
    document = sample_readme()
    assert parse_tool_blocks(document, skip_fenced=False)
    assert "uv sync --all-groups" not in _visible_response_text(document)


def test_native_explicit_markup_is_still_removed_even_with_code_preservation():
    document = "Evidence:\n```bash\necho example\n```\n<tool_call><web_search>test</web_search></tool_call>"
    cleaned = _visible_response_text(document, preserve_code_fences=True)
    assert "echo example" in cleaned
    assert "<tool_call>" not in cleaned


def test_long_readonly_tool_echo_replaces_only_final_duplicate_not_receipts():
    text = sample_readme()
    records = receipts(text)
    rendered = summarize_repeated_tool_output(text, records)
    assert rendered
    assert "exit code `0`" in rendered
    assert len(rendered) < 450
    assert text == records[-1]["output"]
    assert "documented interface 20" in records[-1]["output"]
    assert "documented interface 20" not in rendered


def test_small_output_and_single_tool_are_never_replaced():
    text = sample_readme()
    assert summarize_repeated_tool_output(text, receipts(text)[-1:]) is None
    assert summarize_repeated_tool_output("Ready.", receipts(text)) is None


def test_actual_synthesis_is_not_replaced_with_tool_receipts():
    text = sample_readme()
    answer = "All checks passed. The directory has a Git repository and README."
    assert summarize_repeated_tool_output(answer, receipts(text)) is None
    excerpt = "# Example integration\n\nThe project uses uv and a bounded MCP runtime."
    assert summarize_repeated_tool_output(excerpt, receipts(text)) is None


def test_partial_quote_does_not_trigger_full_document_echo():
    text = sample_readme()
    parts = text.splitlines()
    quote = "\n".join(parts[:22]) + "\n\nMy analysis and new recommendation."
    assert summarize_repeated_tool_output(quote, receipts(text)) is None


def test_mutating_shell_receipts_do_not_trigger_output_replacement():
    text = sample_readme()
    records = [
        {"tool": "bash", "command": "mkdir -p reports", "exit_code": 0, "output": "created"},
        {"tool": "bash", "command": "cat README.md", "exit_code": 0, "output": text},
    ]
    assert summarize_repeated_tool_output(text, records) is None


def test_untrusted_tool_stdout_and_commands_are_never_in_summary():
    text = sample_readme()
    records = receipts(text)
    records[0]["output"] = "PRIVATE_PLACEHOLDER"
    rendered = summarize_repeated_tool_output(text, records)
    assert rendered
    assert "PRIVATE_PLACEHOLDER" not in rendered
    assert "cat README.md" in rendered


def test_explicit_full_output_request_is_never_condensed():
    text = sample_readme()
    for request in (
        "Please paste the complete output verbatim.",
        "Exiba a saída completa de cada comando.",
        "Mostre o texto completo na resposta.",
    ):
        assert summarize_repeated_tool_output(
            text, receipts(text), user_request=request,
        ) is None


def test_unknown_and_missing_exit_status_never_contribute_to_synthetic_receipt():
    text = sample_readme()
    records = receipts(text)
    records[1]["exit_code"] = None
    records[2]["blocked"] = True
    # A mixed/uncertain turn cannot be declared complete by presentation.
    assert summarize_repeated_tool_output(text, records) is None


def test_mixed_effectful_and_read_only_turn_is_never_condensed():
    text = sample_readme()
    records = receipts(text) + [{
        "tool": "bash", "command": "mkdir -p reports", "output": "created",
        "exit_code": 0,
    }]
    assert summarize_repeated_tool_output(text, records) is None
