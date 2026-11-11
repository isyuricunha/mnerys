"""Actual /api/chat_stream through the Gateway composition cutover.

Exercises the real route handler with the composition cutover enabled:
one admitted compose_web_turn call replaces the inline chain, with the
same writers, saves and SSE frames as the legacy path. Synthetic model,
writers and in-memory sessions only.
"""


from types import SimpleNamespace

from core.models import ChatMessage
import pytest

from routes import chat_routes
from src import turn_service as runtime
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_foreground_model_routing import _RouteRequest
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_turn_composer import FakePorts


BASE_CHAT = {"session": "session-1", "mode": "chat"}


class WebRoutePorts(FakePorts):
    def __init__(self):
        super().__init__()
        self.private_writes = []

    def append_user_message(
        self, session, _handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        assert incognito is False
        self.operations.append(("save_user", interaction_mode))
        session.messages.append({
            "role": "user", "content": preprocessed.user_content,
        })
        session.history.append(ChatMessage(
            "user", preprocessed.user_content,
            metadata={"_db_id": f"cutover-user-{len(session.history) + 1}"},
        ))

    def append_private_message(self, session_id, role, content, metadata):
        self.operations.append(("private", session_id, role))
        self.private_writes.append({"role": role, "content": content})
        return {"role": role, "content": content}

    def private_messages(self, session_id):
        return list(self.private_writes)


def cutover_fixture(monkeypatch, store=None):
    admission, session, ports, opts = configured(monkeypatch)
    ports = WebRoutePorts()

    def fake_extract_preset(_preset_id):
        return (None, None, None, None, None, None)

    async def fake_preprocess(message, _att_ids, _sess, *, auto_opened_docs,
                              allow_tool_preprocessing,
                              allow_attachment_preprocessing=False):
        return (message, message, message, [], [])

    chat_handler = SimpleNamespace(
        upload_handler=None,
        validate_and_extract_preset=fake_extract_preset,
        preprocess_message=fake_preprocess,
        update_session_name_if_needed=lambda *_a, **_k: None,
        trim_history_if_needed=lambda *_a, **_k: None,
    )
    writes = []
    if store is None:
        store = SimpleNamespace(
            peek=lambda _id: None, retire_for_session=lambda **k: False,
        )
    gateway = GatewayTurns(TurnService())
    fake_manager = SimpleNamespace(
        get_session=lambda sid: session if sid == session.id else (_ for _ in ()).throw(KeyError(sid)),
        save_sessions=lambda: None,
    )

    def save(current, manager, session_id, text, metrics, **kwargs):
        assert current is session and session_id == session.id
        writes.append(text)
        current.messages.append({"role": "assistant", "content": text})
        current.history.append(ChatMessage(
            "assistant", text,
            metadata={"_db_id": f"cutover-assistant-{len(current.history) + 1}"},
        ))
        return f"cutover-assistant-{len(current.history)}"

    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda req, sid: admission)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(chat_routes, "is_internal_tool_request", lambda request: False)
    monkeypatch.setattr(chat_routes, "_reconcile_selected_route_from_request", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "resolve_session_auth", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "run_post_response_tasks", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "save_assistant_response", save)
    monkeypatch.setattr(chat_routes, "append_interrupted_assistant_response", lambda *args: None)
    monkeypatch.setattr(chat_routes, "gateway_turns", gateway)
    monkeypatch.setattr(chat_routes, "tool_approval_store", store)
    monkeypatch.setattr("src.settings.get_setting", lambda *a, **k: [])
    monkeypatch.setenv("MNERYS_M21_WEB_CORE_CUTOVER", "1")

    def cutover_session():
        def add_message(message):
            session.messages.append({
                "role": message.role, "content": message.content,
            })
        session.add_message = add_message
        session.get_context_messages = lambda *a, **k: []
        return session

    def fake_preface(**_options):
        return ([{"role": "system", "content": "test preface"}], [], [])

    chat_processor = SimpleNamespace(build_context_preface=fake_preface)

    router = chat_routes.setup_chat_routes(
        fake_manager, chat_handler, chat_processor,
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
    )
    routes = {route.path: route.endpoint for route in router.routes}
    return admission, cutover_session(), ports, writes, routes


def chat_request(form, *, privileged=False):
    # M5.3 increment 5: this suite's chat-engine tests run against the
    # PRESERVED plain-chat adapter via the demotion ceiling
    # (can_use_agent=False) — the unified agent-first path never selects a
    # separate engine from a mode label for an agent-permitted caller.
    # Approval-resume tests pass privileged=True: an approval resumes through
    # the agent loop (shell execution is an agent capability).
    privileges = None if privileged else {"can_use_agent": False}
    req = _RouteRequest("chat", privileges=privileges)
    req._form = dict(BASE_CHAT) | form
    return req


def agent_request(form):
    req = _RouteRequest("agent")
    req._form = dict(BASE_CHAT) | {"mode": "agent"} | form
    return req


@pytest.mark.asyncio
async def test_cutover_chat_turn_streams_and_persists_like_legacy(monkeypatch):
    _admission, session, ports, writes, routes = cutover_fixture(monkeypatch)

    async def fake_llm(_candidates, _messages, **_kw):
        yield 'data: {"delta":"Composed answer."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_llm)
    response = await routes["/api/chat_stream"](chat_request({
        "message": "Say hello",
    }))
    frames = [chunk async for chunk in response.body_iterator]
    text = "".join(frames)
    assert '"delta":"Composed answer."' in text
    assert '"type": "message_saved"' in text
    assert "data: [DONE]\n\n" in text
    assert writes == ["Composed answer."]
    assert [item["role"] for item in session.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_cutover_agent_turn_executes_the_offered_tool(monkeypatch):
    _admission, session, ports, writes, routes = cutover_fixture(monkeypatch)

    async def fake_loop(_url, _name, _messages, **kwargs):
        assert kwargs["request_authority"].bound_to(
            owner="alice", session_id=session.id,
        )
        assert kwargs["turn_contract"] is not None
        yield 'data: {"type":"tool_start","tool":"manage_notes","round":1}\n\n'
        yield 'data: {"type":"tool_output","tool":"manage_notes","round":1,"exit_code":0,"output":"PRIVATE"}\n\n'
        yield 'data: {"delta":"Notes listed."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", fake_loop)
    response = await routes["/api/chat_stream"](chat_request(
        {"message": "List my notes", "mode": "agent"}, privileged=True,
    ))
    frames = [chunk async for chunk in response.body_iterator]
    text = "".join(frames)
    assert '"tool_start"' in text
    assert "Notes listed." in text
    assert writes == ["Notes listed."]
    assert [item["role"] for item in session.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_cutover_approval_runs_exact_shell_and_continues(monkeypatch, tmp_path):
    import json
    from src import agent_loop, bg_jobs, containment, tool_execution
    from src.agent_runtime import process_resources
    from src.agent_runtime.authority import RequestAuthority
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    _, session, _, writes, routes = cutover_fixture(monkeypatch, store)
    monkeypatch.setattr("src.gateway_turns.tool_approval_store", store)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session.cwd = str(workspace)
    command = "printf 'web_%s' approval_marker"
    original = RequestAuthority.empty(owner="alice", session_id=session.id, workspace=str(workspace))
    pending = store.create(owner="alice", session_id=session.id, origin_run_id="test-run",
        tool_name="bash", content=command, workspace=str(workspace),
        continuation_query="Inspect the workspace", request_text="Inspect the workspace",
        selected_tools={"bash"}, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", command), request_authority=original)
    monkeypatch.setattr(process_resources, "_LAUNCH_DIR", tmp_path / "private" / "launches")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "private" / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "private" / "jobs")
    monkeypatch.setattr(containment, "_store_path", lambda: tmp_path / "private" / "receipts.json")
    monkeypatch.setattr(containment, "CONTAINMENT_MODE", containment.MODE_REPORT_ONLY)
    monkeypatch.setattr(containment, "MECHANISMS", tuple(m for m in containment.MECHANISMS if m.name == "process_group"))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kw: 10)
    observed = []

    async def model(_candidates, messages, **kwargs):
        observed.append(json.dumps(messages))
        yield 'data: {"delta":"Observed approved output."}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", model)
    response = await routes["/api/chat_stream"](agent_request({
        "message": "", "tool_approval_id": pending.approval_id,
        "tool_approval_decision": "approve_task",
    }))
    text = "".join([chunk async for chunk in response.body_iterator])
    assert "web_approval_marker" in text
    assert observed and "web_approval_marker" in observed[0]
    assert "Observed approved output." in text
    assert len(writes) == 1 and "web_approval_marker" in writes[0]
    assert [item["role"] for item in session.messages] == ["assistant"]
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_cutover_denial_marks_original_card_before_return(monkeypatch):
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action
    store = ToolApprovalStore()
    _, session, _, writes, routes = cutover_fixture(monkeypatch, store)
    monkeypatch.setattr("src.gateway_turns.tool_approval_store", store)
    pending = store.create(owner="alice", session_id=session.id, origin_run_id="test-run",
        tool_name="bash", content="ls -1", workspace=None,
        external_untrusted_context_seen=True, capabilities=capabilities_for_action("bash", "ls -1"))
    marked = []
    monkeypatch.setattr(chat_routes, "_mark_tool_approval_resolved",
        lambda sess, approval_id, decision: marked.append((sess, approval_id, decision)) or True)
    response = await routes["/api/chat_stream"](agent_request({
        "message": "", "tool_approval_id": pending.approval_id,
        "tool_approval_decision": "deny",
    }))
    text = "".join([chunk async for chunk in response.body_iterator])
    assert marked == [(session, pending.approval_id, "deny")]
    assert '"decision": "deny"' in text
    assert store.peek(pending.approval_id) is None and not writes


@pytest.mark.asyncio
async def test_cutover_disabled_by_default_keeps_legacy_chain(monkeypatch):
    _admission, session, ports, writes, routes = cutover_fixture(monkeypatch)
    monkeypatch.setenv("MNERYS_M21_WEB_CORE_CUTOVER", "0")
    composed = []

    async def unexpected(*args, **kwargs):
        composed.append(True)
        yield 'data: {"delta":"x"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(GatewayTurns, "compose_web_turn", unexpected)

    async def fake_llm(_candidates, _messages, **_kw):
        yield 'data: {"delta":"Legacy answer."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_llm)
    response = await routes["/api/chat_stream"](chat_request({
        "message": "Say hello",
    }))
    frames = [chunk async for chunk in response.body_iterator]
    text = "".join(frames)
    assert '"delta":"Legacy answer."' in text
    assert writes == ["Legacy answer."]
    assert not composed
