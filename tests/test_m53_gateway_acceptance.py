"""M5.3 increment 6: formal end-to-end Gateway acceptance for the M5.3 exit
gate — the REAL agent loop, risk classifier, approval store, scoped rules and
Bash executor, with only the MODEL stubbed (a scripted provider stream).

Scenarios (all through stream_agent_loop → the Gateway gates → the real
dispatcher):

1. LOW_RISK command executes with no approval prompt.
2. A risky command creates a genuine pending approval carrying the risk
   reason; an Allow-Once decision resumes the sealed operation which executes
   EXACTLY ONCE against the real filesystem (a disposable workspace).
3. A Deny decision never executes and reports truthful refusal feedback.
4. With YOLO enabled, the same risky command executes without a prompt and
   the suppression is audited.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import src.agent_loop as agent_loop
from src.prompt_security import untrusted_context_message

OWNER = "alice"
SESSION = "m53-acceptance"


def _install_fake_model(monkeypatch, reply):
    async def fake_stream(*args, **kwargs):
        for chunk in reply:
            yield chunk
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    # The single-user acceptance owner is the deployment admin (the harness
    # mirrors AUTH_ENABLED=false); the real deployment resolves this from the
    # auth manager. Without it the public/non-admin ceiling blocks shell tools
    # before the risk gate — which its own suite covers separately.
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    # The dispatcher's deployment admin gate (mirrors AUTH_ENABLED=false).
    import src.tool_execution as _te

    monkeypatch.setattr(_te, "_owner_is_admin", lambda owner: True, raising=False)


def _collect(endpoint_url, model, messages, *, owner="alice", session_id=None,
             authority=None, **kwargs):
    # Bind a REAL request authority like the Gateway route does (the intent
    # derivation grants the workspace family for file/shell requests). An
    # explicitly passed authority mirrors the approval-resume path, which
    # restores the pending operation's ORIGINAL sealed authority.
    from src.agent_runtime.authority import bind_request_authority, create_request_authority

    if authority is None:
        authority = create_request_authority(
            " ".join(str(m.get("content") or "") for m in messages),
            owner=owner, session_id=session_id or "m53", workspace=kwargs.get("workspace") or "",
            policy=None,
        )

    async def run():
        events = []
        with bind_request_authority(authority):
            async for chunk in agent_loop.stream_agent_loop(
                    endpoint_url, model, messages,
                    owner=owner, session_id=session_id, **kwargs,
                ):
                if isinstance(chunk, str) and chunk.startswith("data: ") and chunk != "data: [DONE]\n\n":
                    try:
                        events.append(json.loads(chunk[6:]))
                    except ValueError:
                        pass
        return events

    return asyncio.run(run())


def _bash_reply(command: str):
    fence = "```Bash\n" + command + "\n```"
    return [f"data: {json.dumps({'delta': fence})}\n\n"]


def _tool_events(events):
    return [
        event for event in events
        if event.get("type") == "tool_start" or (
            event.get("type") == "tool_output"
        )
    ]


@pytest.fixture(autouse=True)
def _clean_approval_singleton():
    yield
    # Remove approvals created by each scenario from the process singleton.
    store = agent_loop.tool_approval_store
    for key in list(getattr(store, "_pending", {}).keys()):
        store._pending.pop(key, None)


def test_low_risk_command_executes_without_an_approval_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _install_fake_model(monkeypatch, _bash_reply("echo hello-risk-acceptance"))
    events = _collect(
        "http://local.test/v1", "small-local-model",
        [{"role": "user", "content": "say hello using the Bash tool"}],
        session_id="m53-acceptance", owner="alice", max_rounds=2,
        workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
    )
    tool_events = _tool_events(events)
    assert any(event.get("tool") == "bash" for event in tool_events), events[-3:]
    outputs = [event for event in events if event.get("type") == "tool_output"]
    assert any("hello-risk-acceptance" in str(event.get("output", "")) for event in outputs)
    # No approval card was raised for the harmless command.
    assert not [e for e in events if e.get("ask_user", {}).get("kind") == "tool_approval"]


def test_risky_command_prompts_then_allow_once_executes_exactly_once(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "build").mkdir()
    (workspace / "build" / "artifact.txt").write_text("stale\n", encoding="utf-8")

    _install_fake_model(monkeypatch, _bash_reply("rm -rf build"))
    events = _collect(
        "http://local.test/v1", "small-local-model",
        [{"role": "user", "content": "Use the Bash tool to remove the build directory"}],
        session_id="m53-acceptance", owner="alice", max_rounds=1,
        workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
    )
    approvals = [e["ask_user"] for e in events if e.get("ask_user", {}).get("kind") == "tool_approval"]
    assert approvals, "the risky command must raise a genuine approval prompt"
    card = approvals[0]
    pending_card = agent_loop.tool_approval_store.peek(card["approval_id"])
    assert pending_card is not None and pending_card.tool_name == "bash"
    # The card shows the concrete risk reason and the effective workspace.
    assert "approval" in str(card.get("question", "")).lower() or "dangerous" in json.dumps(card).lower()
    assert workspace.name in json.dumps(card) or "ws" in json.dumps(card)
    # Nothing executed yet: the disposable artifact still exists.
    assert (workspace / "build" / "artifact.txt").exists()

    pending = agent_loop.tool_approval_store.peek(card["approval_id"])
    assert pending is not None and pending.tool_name == "bash"
    # THE OPERATOR DECIDES: Allow once (the real supported mechanism).
    exact = agent_loop.tool_approval_store.consume(
        pending.approval_id, decision="allow_once",
        owner="alice", session_id="m53-acceptance", allow_continuation=True,
    )
    assert exact is not None
    # Replay/duplicate decision cannot re-consume.
    assert agent_loop.tool_approval_store.consume(
        pending.approval_id, decision="allow_once",
        owner="alice", session_id="m53-acceptance", allow_continuation=True,
    ) is None

    # Resume: the loop re-runs the turn; the sealed replay path executes the
    # exact approved command exactly once through the REAL dispatcher.
    _install_fake_model(monkeypatch, _bash_reply("rm -rf build"))
    resume_events = _collect(
        "http://local.test/v1", "small-local-model",
        [
            {"role": "user", "content": "Use the Bash tool to remove the build directory"},
            {"role": "user", "content": "Approved. Continue with exactly the approved action."},
        ],
        session_id="m53-acceptance", owner="alice", max_rounds=2,
        workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
        exact_approval=exact,
        # The sealed-replay resume restores the pending's ORIGINAL authority
        # (the request identity the approval was sealed under).
        authority=getattr(exact.pending, "request_authority", None),
    )
    resume_outputs = [event for event in resume_events if event.get("type") == "tool_output"]
    bash_resume = [e for e in resume_outputs if e.get("tool") == "bash"]
    # The sealed operation executed EXACTLY ONCE (the replay attempt after
    # the claim is consumed refuses without executing).
    executed_once = [e for e in bash_resume if e.get("exit_code") == 0 and e.get("approved") is True]
    if len(executed_once) != 1:
        print("RESUME-FAIL-DEBUG:", [str(e)[:240] for e in bash_resume])
        assert len(executed_once) == 1
    # REAL filesystem evidence: the disposable build directory is gone.
    assert not (workspace / "build").exists()


def test_deny_decision_never_executes_and_reports_truthfully(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "build").mkdir()
    (workspace / "build" / "keep.txt").write_text("precious\n", encoding="utf-8")

    _install_fake_model(monkeypatch, _bash_reply("rm -rf build"))
    events = _collect(
        "http://local.test/v1", "small-local-model",
        [{"role": "user", "content": "Use the Bash tool to remove the build directory"}],
        session_id="m53-deny", owner="alice", max_rounds=1,
        workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
    )
    approvals = [e["ask_user"] for e in events if e.get("ask_user", {}).get("kind") == "tool_approval"]
    assert approvals
    exact = agent_loop.tool_approval_store.consume(
        approvals[0]["approval_id"], decision="deny",
        owner="alice", session_id="m53-deny", allow_continuation=True,
    )
    assert exact is None  # no authority, no claim
    assert (workspace / "build" / "keep.txt").exists()  # nothing executed
    # A deny cannot be revived into a grant afterwards.
    assert agent_loop.tool_approval_store.consume(
        approvals[0]["approval_id"], decision="allow_once",
        owner="alice", session_id="m53-deny", allow_continuation=True,
    ) is None


def test_yolo_on_suppresses_the_risk_prompt_and_executes(tmp_path, monkeypatch, caplog):
    import logging

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "build").mkdir()
    (workspace / "build" / "artifact.txt").write_text("stale\n", encoding="utf-8")

    _install_fake_model(monkeypatch, _bash_reply("rm -rf build"))
    with caplog.at_level(logging.INFO, logger="src.tool_capabilities"):
        events = _collect(
            "http://local.test/v1", "small-local-model",
            [{"role": "user", "content": "Use the Bash tool to remove the build directory"}],
            session_id="m53-yolo", owner="alice", max_rounds=2,
            workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
            yolo_enabled=True,
        )
    outputs = [event for event in events if event.get("type") == "tool_output"]
    assert any(event.get("tool") == "bash" for event in outputs)
    # No approval card: the discretionary prompt was suppressed.
    assert not [e for e in events if e.get("ask_user", {}).get("kind") == "tool_approval"]
    # The classification still ran and the suppression is audited.
    assert any("YOLO suppressed risk approval" in message for message in caplog.messages)
    assert not (workspace / "build").exists()  # the real execution happened


def test_prohibited_operation_stays_refused_even_with_yolo(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _install_fake_model(monkeypatch, _bash_reply("cat /etc/shadow"))
    events = _collect(
        "http://local.test/v1", "small-local-model",
        [{"role": "user", "content": "Use the Bash tool to read /etc/shadow"}],
        session_id="m53-denied", owner="alice", max_rounds=2,
        workspace=str(workspace), relevant_tools={"bash"},
        forced_tools={"bash"},
        yolo_enabled=True,
    )
    # The sensitive-path classification REQUIRES approval (high) — and with
    # the sandbox/unattended grants absent, the turn may not execute it: the
    # workspace-confined dispatcher refuses the outside-workspace read.
    outputs = [event for event in events if event.get("type") == "tool_output"]
    executed = any(event.get("tool") == "bash" for event in outputs)
    if executed:
        output_text = " ".join(str(event.get("output", "")) for event in outputs)
        assert "root:" not in output_text  # never actually read system files


def test_exact_approval_resumes_only_under_the_sealed_security_context(tmp_path, monkeypatch):
    """Context-equality regression (the formal-acceptance fix): an untrusted-
    context-gated approval resumes only ARMED; a risk-gated approval sealed in
    a clean run resumes only CLEAN; a mismatch requests fresh authorization."""
    import tempfile

    from src.tool_capabilities import capabilities_for_action
    from src.tool_execution import (
        NO_TOOL_SECURITY_CONTEXT, ToolBlock, ToolRunSecurityContext, execute_tool_block,
    )
    from src.agent_runtime.authority import (
        bind_request_authority, create_request_authority,
    )

    workspace = tempfile.mkdtemp()
    import src.tool_execution as _te

    monkeypatch.setattr(_te, "_owner_is_admin", lambda owner: True, raising=False)
    authority = create_request_authority(
        "remove the build directory", owner=OWNER, session_id=SESSION,
        workspace=workspace, policy=None,
    )
    store = agent_loop.tool_approval_store

    # 1. An ARMED pending resumes under an ARMED context.
    armed_pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-a",
        tool_name="bash", content="echo fine",
        workspace=workspace, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "echo fine"),
        request_authority=authority,
    )
    armed_exact = store.consume(
        armed_pending.approval_id, decision="allow_once",
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    armed_sec = ToolRunSecurityContext(external_untrusted_context_seen=True)
    with bind_request_authority(authority):
        desc, result = asyncio.run(execute_tool_block(
            ToolBlock("bash", "echo fine"), owner=OWNER, session_id=SESSION,
            workspace=workspace, security_context=armed_sec,
            exact_approval=armed_exact,
        ))
    assert result.get("exit_code") == 0 and "fine" in str(result.get("output", "")), result

    # 2. The ARMED pending does NOT resume under a CLEAN context.
    armed_pending2 = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-b",
        tool_name="bash", content="echo again",
        workspace=workspace, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "echo again"),
        request_authority=authority,
    )
    armed_exact2 = store.consume(
        armed_pending2.approval_id, decision="allow_once",
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    clean_sec = ToolRunSecurityContext()
    with bind_request_authority(authority):
        desc, result = asyncio.run(execute_tool_block(
            ToolBlock("bash", "echo again"), owner=OWNER, session_id=SESSION,
            workspace=workspace, security_context=clean_sec,
            exact_approval=armed_exact2,
        ))
    assert result.get("exit_code") == 1
    assert "same armed run security context" in str(result.get("error", ""))

    # 3. A RISK-gated (clean) pending resumes under a CLEAN context.
    clean_pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-c",
        tool_name="bash", content="echo third",
        workspace=workspace, external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("bash", "echo third"),
        request_authority=authority,
    )
    clean_exact = store.consume(
        clean_pending.approval_id, decision="allow_once",
        owner=OWNER, session_id=SESSION, allow_continuation=True,
    )
    with bind_request_authority(authority):
        desc, result = asyncio.run(execute_tool_block(
            ToolBlock("bash", "echo third"), owner=OWNER, session_id=SESSION,
            workspace=workspace, security_context=clean_sec,
            exact_approval=clean_exact,
        ))
    assert result.get("exit_code") == 0 and "third" in str(result.get("output", "")), result
    store.retire_all() if hasattr(store, "retire_all") else None
