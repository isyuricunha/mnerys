"""Agent final answers use post-tool text, never accumulated progress narration.

All fixtures are synthetic; no private transcript or provider access is needed.
"""
from src.agent_runtime.tool_output_presentation import (
    completed_tool_round_answer, should_emit_post_tool_final,
)


def test_terminal_reconciliation_event_is_emitted_even_if_no_postprocessing_change():
    summary = "Final summary with only verified findings."
    assert should_emit_post_tool_final(
        summary, summary,
        selected_model_round=True,
        has_tool_events=True,
        preemptive_final_already_sent=False,
    )


def test_terminal_reconciliation_event_is_not_sent_if_not_needed():
    summary = "Final summary."
    params = dict(
        response=summary,
        before_summary=summary,
        selected_model_round=False,
        has_tool_events=True,
        preemptive_final_already_sent=False,
    )
    assert not should_emit_post_tool_final(**params)
    assert not should_emit_post_tool_final(**{**params, "has_tool_events": False})
    assert not should_emit_post_tool_final(**{
        **params, "selected_model_round": True,
        "preemptive_final_already_sent": True,
    })
    assert not should_emit_post_tool_final(**{
        **params, "selected_model_round": True, "response": " ",
    })
    assert should_emit_post_tool_final(**{
        **params, "before_summary": "Older answer.",
    })


def receipts(*rounds):
    return [
        {"round": index, "tool": "glob", "exit_code": 0, "output": "synthetic"}
        for index in rounds
    ]


def test_model_round_is_selected_before_late_cleanup_can_change_stream_suffix():
    rounds = [
        "I will inspect the files before answering.",
        "The CLI entry point is in server.py.",
    ]
    accumulator = (
        "I will inspect the files before answering.\n\n"
        "<tool_call hidden=true>read_file</tool_call>\n\n"
        "The CLI entry point is in server.py."
    )
    # The real model's terminal round is an exact suffix of its raw stream.
    assert completed_tool_round_answer(
        accumulator, rounds, receipts(1),
    ) == rounds[-1]
    # A later server-authored notice must not be silently discarded.
    assert completed_tool_round_answer(
        accumulator + "\n\nThis operation was denied by policy.", rounds, receipts(1),
    ) is None


def test_selection_requires_a_completed_post_tool_round():
    rounds = ["The agent used tools and only provided progress."]
    assert completed_tool_round_answer(
        "progress", rounds, receipts(1),
    ) is None
    assert completed_tool_round_answer(
        "progress", rounds + [""], receipts(1),
    ) is None


def test_realistic_six_round_execution_has_only_one_terminal_summary():
    # Mirrors the sanitized *structure* observed in a recent preview:
    # five tool rounds, mostly blank preambles, followed by synthesis.
    rounds = [
        "", "", "I found files; now checking definitions.", "", "",
        "Resumo: Glob listed Python files; Grep found four entry points; "
        "Read returned the project's build metadata.",
    ]
    events = [
        {"round": n, "tool": ("get_workspace" if n == 1 else "glob"),
         "output": "synthetic", "exit_code": 0}
        for n in range(1, 6)
    ]
    accumulated = (
        "I'll start by finding the project files. "
        "I found files; now checking definitions. "
        "Now I will inspect package metadata. "
        + rounds[-1]
    )
    final = completed_tool_round_answer(accumulated, rounds, events)
    assert final == rounds[-1]
    assert "I'll start" not in final
    assert should_emit_post_tool_final(
        final, final, selected_model_round=True, has_tool_events=True,
        preemptive_final_already_sent=False,
    )
    from routes.chat_routes import _AgentRenderState
    state = _AgentRenderState()
    for n in range(1, 6):
        state.consume({"type": "agent_step", "round": n})
        state.consume({"delta": "Synthetic progress."})
        state.consume({"type": "tool_start", "round": n})
        state.consume({"type": "tool_output", "round": n, "exit_code": 0})
    state.consume({"type": "agent_step", "round": 6})
    state.consume({"delta": rounds[-1]})
    event = state.consume({
        "type": "final_response", "content": final,
        "render_owner": "streamed", "replacement_scope": "round",
    })
    assert event["content"] == rounds[-1]
    assert event["replacement_scope"] == "round"
    assert state.finish(final) == rounds[-1]
    metadata = state.metadata({"round_texts": list(rounds), "tool_events": list(events)})
    assert metadata["round_texts"] == rounds
    assert metadata["round_texts"][-1] != accumulated


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


def test_existing_complete_answer_still_emits_a_scoped_reconciliation():
    rounds = ["Earlier progress.", "Final answer."]
    # The Core already holds only final prose, but the Web has streamed
    # an earlier progress bubble and still needs a final round boundary.
    assert completed_tool_round_answer("Final answer.", rounds, receipts(1)) == "Final answer."


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
