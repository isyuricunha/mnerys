"""Agent final answers use post-tool text, never accumulated progress narration.

All fixtures are synthetic; no private transcript or provider access is needed.
"""
from src.agent_runtime.tool_output_presentation import completed_tool_round_answer


def receipts(*rounds):
    return [
        {"round": index, "tool": "glob", "exit_code": 0, "output": "synthetic"}
        for index in rounds
    ]


def test_cumulative_progress_is_not_pasted_before_final_summary():
    rounds = [
        "I'll start by listing project sources.",
        "The first files were found. I'll inspect entry points.",
        "The entry points are listed. I'll inspect package metadata.",
        "Here is the final summary. Glob located sources, Grep found entry points, "
        "and Read found the project's Python requirement.",
    ]
    original = "\n\n".join(rounds)
    events = receipts(1, 2, 3)
    answer = completed_tool_round_answer(original, rounds, events)
    assert answer == rounds[3]
    assert "I'll start" not in answer
    assert "I'll inspect" not in answer
    assert rounds[0] == "I'll start by listing project sources."
    assert events == receipts(1, 2, 3)


def test_final_synthesis_is_kept_even_when_it_contains_a_short_intro():
    rounds = [
        "I will inspect the files now.",
        "I have all the evidence. The CLI entry point is in server.py.",
    ]
    answer = completed_tool_round_answer(
        "\n\n".join(rounds), rounds, receipts(1),
    )
    assert answer == rounds[-1]


def test_last_round_after_tools_must_be_real():
    rounds = ["Running checks.", "Still running checks."]
    assert completed_tool_round_answer(
        "\n\n".join(rounds), rounds, receipts(2),
    ) is None


def test_tool_only_completion_never_drops_tool_result_or_previous_text():
    rounds = ["Planning.", ""]
    assert completed_tool_round_answer(
        "Planning.\n\n", rounds, receipts(1, 2),
    ) is None


def test_mismatching_final_text_preserves_policy_postprocessing():
    rounds = ["Planning.", "Success."]
    assert completed_tool_round_answer(
        "A server-generated policy warning superseded the response.",
        rounds, receipts(1),
    ) is None


def test_no_tool_calls_or_unknown_rounds_cannot_rewrite_prose():
    rounds = ["Opening note.", "Result."]
    whole = "\n\n".join(rounds)
    assert completed_tool_round_answer(whole, rounds, []) is None
    assert completed_tool_round_answer(
        whole, rounds, [{"tool": "read_file", "exit_code": 0}],
    ) is None


def test_mixed_numbered_and_un_numbered_tool_receipts_keep_existing_answer():
    rounds = ["Start inspection.", "Final findings."]
    combined = "\\n\\n".join(rounds)
    events = receipts(1) + [{"tool": "read_file", "exit_code": 0}]
    assert completed_tool_round_answer(combined, rounds, events) is None


def test_existing_complete_answer_is_unchanged():
    rounds = ["Earlier progress.", "Final answer."]
    assert completed_tool_round_answer("Final answer.", rounds, receipts(1)) is None


def test_only_positive_integer_round_ids_can_authorize_last_round_selection():
    rounds = ["Plan.", "Answer."]
    whole = "\n\n".join(rounds)
    for invalid in (None, "1", 0, -1, True):
        assert completed_tool_round_answer(
            whole, rounds,
            [{"round": invalid, "tool": "bash", "exit_code": 0}],
        ) is None


def test_no_heuristic_splitting_of_untrusted_code_or_quotes():
    final = (
        "Example text: 'I'll start by inspecting files.'\n\n"
        "```bash\npython -m pytest\n```\n\n"
        "The snippet above is documentation, not an action."
    )
    rounds = ["Inspect source.", final]
    assert completed_tool_round_answer(
        "\n\n".join(rounds), rounds, receipts(1),
    ) == final


def test_web_adapter_preserves_earlier_narration_but_finalizes_last_round():
    from routes.chat_routes import _AgentRenderState

    state = _AgentRenderState()
    state.consume({"delta": "I will find project files.", "round": 1})
    state.consume({"type": "tool_start", "round": 1})
    state.consume({"type": "tool_output", "round": 1})
    state.consume({"type": "agent_step", "round": 2})
    state.consume({"delta": "I found files. Now inspect definitions.", "round": 2})
    state.consume({"type": "tool_start", "round": 2})
    state.consume({"type": "tool_output", "round": 2})
    state.consume({"type": "agent_step", "round": 3})
    state.consume({"delta": "The project has four entry points.", "round": 3})
    rounds = [
        "I will find project files.",
        "I found files. Now inspect definitions.",
        "The project has four entry points.",
    ]
    full = "\\n\\n".join(rounds)
    canonical = completed_tool_round_answer(full, rounds, receipts(1, 2))
    event = state.consume({"type": "final_response", "content": canonical})
    assert event["content"] == rounds[-1]
    assert event["replacement_scope"] == "round"
    assert event["render_owner"] == "streamed"
    assert state.finish(full) == rounds[-1]
    assert state.metadata()["replacement_scope"] == "round"


def test_round_selection_is_not_limited_to_portuguese_or_read_only_tools():
    rounds = [
        "I will write the requested file now.",
        "The file was written and verified.",
    ]
    assert completed_tool_round_answer(
        "\n\n".join(rounds), rounds,
        [{"round": 1, "tool": "write_file", "exit_code": 0}],
    ) == rounds[-1]
