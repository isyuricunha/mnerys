"""Exercise the actual Agent Core stream with mocked native model/tool events.

The test asserts a *transported* final round, not only a helper result.
No network, credentials, filesystem mutation, or user data is involved.
"""
import asyncio
import json

import pytest

import src.agent_loop as agent
from src.tool_capabilities import ToolGateDecision


@pytest.mark.asyncio
async def test_real_agent_loop_emits_only_post_tool_round_as_final(monkeypatch):
    monkeypatch.setattr(agent, "get_setting", lambda k, default=None: default)
    monkeypatch.setattr(agent, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent, "estimate_tokens", lambda *a, **kw: 10)
    monkeypatch.setattr(agent, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        agent.ToolRunSecurityContext, "decision_for",
        lambda self, *a, **kw: ToolGateDecision(True),
    )

    actual_calls = []
    async def fake_tool(block, *a, **kw):
        actual_calls.append(block.tool_type)
        return block.tool_type, {
            "output": "synthetic observed output", "exit_code": 0,
        }
    monkeypatch.setattr(agent, "execute_tool_block", fake_tool)

    model_rounds = [
        ("I'll inspect the project files.", "Glob", {"pattern": "**/*.py"}),
        ("Now I'll search for definitions.", "Grep", {"pattern": "def main", "glob": "**/*.py"}),
        ("Resumo: identifiquei o workspace e os arquivos Python usando "
         "as ferramentas Glob e Grep.", None, None),
    ]
    seen = {"rounds": 0}

    async def fake_model(_candidates, messages, **kwargs):
        current = seen["rounds"]
        assert current < len(model_rounds), current
        seen["rounds"] += 1
        content, tool, arguments = model_rounds[current]
        yield "data: " + json.dumps({"delta": content}) + "\n\n"
        if tool:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": f"call-{current+1}", "name": tool,
                           "arguments": json.dumps(arguments)}],
            }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent, "stream_llm_with_fallback", fake_model)
    output = []
    async for chunk in agent.stream_agent_loop(
        "https://example.com/v1", "gpt-4o",
        [{"role": "user", "content":
          "Encontre todos os arquivos .py. Pesquise por def main e leia "
          "o arquivo pyproject.toml. Apresente um resumo."}],
        owner="admin", max_rounds=3, max_tool_calls=5,
        relevant_tools={"glob", "grep"},
    ):
        if isinstance(chunk, str) and chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
            try:
                output.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass

    assert actual_calls == ["glob", "grep"]
    final = model_rounds[-1][0]
    corrections = [ev for ev in output if ev.get("type") == "final_response"]
    assert corrections, "Agent Core must explicitly reconcile streamed pre-tool prose"
    assert corrections[-1]["content"] == final, {
        "model_rounds_seen": seen["rounds"],
        "event_types": [e.get("type") or ("delta" if "delta" in e else "unknown") for e in output],
        "finals": [e.get("content") for e in corrections],
        "rounds": [m.get("round_texts") for m in (
            [ev.get("data") for ev in output if ev.get("type") == "metrics"]
        ) if isinstance(m, dict)],
    }
    assert corrections[-1]["replacement_scope"] == "round"
    assert corrections[-1]["render_owner"] == "streamed"
    assert "I'll inspect" not in corrections[-1]["content"]
    metrics = [ev["data"] for ev in output if ev.get("type") == "metrics"]
    assert metrics, "Expected persistent round ledger"
    recorded = metrics[-1]["round_texts"]
    assert len(recorded) >= 3
    assert recorded[-1] == final
    assert "I'll inspect" not in recorded[-1]
    from routes.chat_routes import _AgentRenderState

    web_state = _AgentRenderState()
    for event in output:
        web_state.consume(event)
    assert web_state.content == final
    assert web_state.metadata()["replacement_scope"] == "round"
    assert web_state.finish(final) == final
    assert web_state.message_saved(1)["replacement_scope"] == "round"
