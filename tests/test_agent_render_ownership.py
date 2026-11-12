"""Execute the route's production state machine without importing app services.

AST loading isolates the dependency-free class; assertions are event/content
behavior, never source text or a reimplementation of the routing logic.
Also runnable with `python tests/test_agent_render_ownership.py`.
"""
import ast
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / "routes/chat_routes.py"
tree = ast.parse(path.read_text())
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_AgentRenderState")
namespace = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
State = namespace["_AgentRenderState"]


class AgentRenderOwnershipTests(unittest.TestCase):
    def test_plain_deltas_append_and_save_streamed_owner(self):
        state = State()
        self.assertEqual(state.consume({"delta": "Hello "})["render_owner"], "streamed")
        state.consume({"delta": "world"})
        self.assertEqual(state.content, "Hello world")
        self.assertEqual(state.metadata({"tokens": 2}), {"tokens": 2, "render_owner": "streamed"})
        self.assertEqual(state.message_saved(42), {"type": "message_saved", "id": 42, "render_owner": "streamed"})

    def test_final_snapshot_replaces_draft_and_persists_ownership(self):
        state = State()
        state.consume({"delta": "Draft notes"})
        event = state.consume({"type": "final_response", "content": "Canonical notes"})
        self.assertEqual(state.content, "Canonical notes")
        self.assertEqual(event["render_owner"], "structured")
        self.assertEqual(event["replacement_scope"], "turn")
        metrics = state.metadata({"tool_events": [{"tool": "manage_notes"}]})
        self.assertEqual(metrics["render_owner"], "structured")
        self.assertEqual(metrics["replacement_scope"], "turn")
        self.assertEqual(state.message_saved(1)["render_owner"], metrics["render_owner"])
        self.assertEqual(state.message_saved(1)["replacement_scope"], "turn")

    def test_later_synthesis_replaces_intermediate_snapshot_and_is_not_dropped(self):
        state = State()
        state.consume({"type": "final_response", "content": "Intermediate list"})
        state.consume({"type": "agent_step", "round": 2})
        first = state.consume({"delta": "Synthesis "})
        second = state.consume({"delta": "continues"})
        self.assertEqual(first["render_owner"], "streamed")
        self.assertEqual(first["replacement_scope"], "turn")
        self.assertNotIn("replacement_scope", second)
        self.assertEqual(state.content, "Synthesis continues")
        self.assertEqual(state.metadata()["render_owner"], "streamed")
        self.assertEqual(state.message_saved(1)["render_owner"], "streamed")
        self.assertEqual(state.message_saved(1)["replacement_scope"], "turn")

    def test_thinking_and_empty_deltas_do_not_take_answer_ownership(self):
        state = State()
        state.consume({"type": "final_response", "content": "Notes"})
        event = state.consume({"delta": "Reasoning", "thinking": True})
        state.consume({"delta": ""})
        self.assertEqual(event["render_owner"], "structured")
        self.assertEqual(state.content, "Notes")
        self.assertEqual(state.message_saved(1)["render_owner"], "structured")

    def test_final_correction_after_synthesis_wins_without_duplicate_content(self):
        state = State()
        for event in [
            {"delta": "Draft"},
            {"type": "final_response", "content": "List"},
            {"delta": "Synthesis"},
            {"type": "final_response", "content": "Corrected answer"},
            {"type": "final_response", "content": "Corrected answer"},
        ]:
            state.consume(event)
        self.assertEqual(state.content, "Corrected answer")
        self.assertEqual(state.metadata()["render_owner"], "structured")

    def test_explicit_streamed_final_and_delta_form_snapshot(self):
        state = State()
        state.consume({"delta": "Old"})
        event = state.consume({"type": "final_response", "delta": "Replacement", "render_owner": "streamed"})
        self.assertEqual(event["replacement_scope"], "turn")
        self.assertEqual(state.content, "Replacement")
        self.assertEqual(state.metadata()["render_owner"], "streamed")

    def test_multi_round_shell_narration_stays_before_tools_and_final_is_last_round(self):
        state = State()
        first = state.consume({"delta": "Checking the folder."})
        state.consume({"type": "tool_start", "tool": "bash", "round": 1})
        state.consume({"type": "tool_output", "tool": "bash", "round": 1, "exit_code": 0})
        state.consume({"type": "agent_step", "round": 2})
        state.consume({"delta": "Checking Git."})
        state.consume({"type": "tool_start", "tool": "bash", "round": 2})
        state.consume({"type": "tool_output", "tool": "bash", "round": 2, "exit_code": 128})
        state.consume({"type": "agent_step", "round": 3})
        state.consume({"delta": "Git failed; README is missing."})
        final = state.consume({
            "type": "final_response",
            "content": "Checking the folder.Checking Git.Git failed; README is missing.",
        })
        self.assertEqual(first["render_owner"], "streamed")
        self.assertEqual(final["content"], "Git failed; README is missing.")
        self.assertEqual(final["render_owner"], "streamed")
        self.assertEqual(final["replacement_scope"], "round")
        self.assertEqual(state.content, "Git failed; README is missing.")
        self.assertEqual(state.metadata()["replacement_scope"], "round")
        self.assertEqual(state.message_saved(12)["replacement_scope"], "round")

    def test_core_explicit_round_scope_preserves_final_when_cleaned_text_is_not_suffix(self):
        state = State()
        state.consume({"delta": "I will inspect project files."})
        state.consume({"type": "tool_start", "round": 1})
        state.consume({"type": "tool_output", "round": 1, "exit_code": 0})
        state.consume({"type": "agent_step", "round": 2})
        state.consume({"delta": "Draft with provider artifact formatting."})
        event = state.consume({
            "type": "final_response",
            "content": "Final clean project summary.",
            "render_owner": "streamed",
            "replacement_scope": "round",
        })
        self.assertEqual(event["content"], "Final clean project summary.")
        self.assertEqual(event["replacement_scope"], "round")
        self.assertEqual(state.content, "Final clean project summary.")
        self.assertEqual(state.metadata()["replacement_scope"], "round")
        self.assertEqual(state.message_saved(1)["replacement_scope"], "round")

    def test_unproven_explicit_round_scope_does_not_override_no_tool_or_last_tool(self):
        for tool_in_last_round in (False, True):
            state = State()
            if tool_in_last_round:
                state.consume({"type": "tool_start", "round": 1})
            event = state.consume({
                "type": "final_response",
                "content": "A replacement.",
                "render_owner": "streamed",
                "replacement_scope": "round",
            })
            self.assertEqual(event["replacement_scope"], "turn")

    def test_done_without_cumulative_snapshot_preserves_last_round_only(self):
        state = State()
        state.consume({"delta": "Running pwd..."})
        state.consume({"type": "tool_start", "round": 1})
        state.consume({"type": "agent_step", "round": 2})
        state.consume({"delta": "Final results."})
        answer = state.finish("Running pwd...Final results.")
        self.assertEqual(answer, "Final results.")
        self.assertEqual(state.metadata()["replacement_scope"], "round")

    def test_no_post_tool_synthesis_never_discards_partial_result(self):
        state = State()
        state.consume({"delta": "Inspection started."})
        state.consume({"type": "tool_start", "round": 1})
        state.consume({"type": "tool_output", "round": 1, "exit_code": 1})
        self.assertEqual(state.finish("Inspection started."), "Inspection started.")
        self.assertNotIn("replacement_scope", state.metadata())

    def test_canonical_override_not_matching_last_round_keeps_existing_semantics(self):
        state = State()
        state.consume({"delta": "Before."})
        state.consume({"type": "tool_start", "round": 1})
        state.consume({"type": "agent_step", "round": 2})
        state.consume({"delta": "Draft."})
        event = state.consume({
            "type": "final_response",
            "content": "Corrected independent canonical answer.",
            "render_owner": "structured",
        })
        self.assertEqual(event["replacement_scope"], "turn")
        self.assertEqual(event["render_owner"], "structured")
        self.assertEqual(state.content, "Corrected independent canonical answer.")

    def test_web_agent_loop_passes_through_progress_between_start_and_output(self):
        """Inspect the production event-family tuple, not a duplicated test list."""
        event_tuples = []
        for item in ast.walk(tree):
            if not isinstance(item, ast.Tuple):
                continue
            names = {
                child.value for child in item.elts
                if isinstance(child, ast.Constant) and type(child.value) is str
            }
            if {"tool_start", "tool_output", "agent_step"} <= names:
                event_tuples.append(names)
        self.assertTrue(event_tuples, "Expected the Web Agent SSE dispatch family")
        self.assertTrue(all("tool_progress" in names for names in event_tuples))


if __name__ == "__main__":
    unittest.main()
