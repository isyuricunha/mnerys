"""M2.1 strict Web/Core persisted text uploads: synthetic HTTP and real ports."""

from core.models import ChatMessage
from fastapi import FastAPI
import httpx
import pytest

from routes import gateway_text_adapter
from src import agent_runs, turn_service as runtime
from src.gateway_admission import AdmissionDenied
from src.gateway_turns import GatewayTurns
from src.gateway_web_text_cutover import parse_strict_web_text_attachment_ids
from src.turn_service import TurnService
from tests.test_mnerys_gateway_direct_attachments import (
    ATTACHMENT_ID, CONTENT, AttachmentPorts, configured,
)
from tests.test_mnerys_gateway_web_post_response import cleanup_run
from tests.test_mnerys_gateway_web_text_cutover import eligible


class PersistAttachmentPorts(AttachmentPorts):
    def append_user_message(
        self, session, handler, preprocessed, *, incognito,
        interaction_mode, auto_escalated,
    ):
        assert not incognito and not auto_escalated
        assert interaction_mode == "chat"
        assert preprocessed.user_content == "Summarize the attached note."
        assert CONTENT not in preprocessed.user_content
        assert [item["id"] for item in preprocessed.attachment_meta] == [ATTACHMENT_ID]
        self.operations.append("save_user")
        session.messages.append({"role": "user", "content": preprocessed.user_content})
        session.history.append(ChatMessage(
            "user", preprocessed.user_content,
            metadata={"_db_id": "fixture-user-1", "attachments": preprocessed.attachment_meta},
        ))


def arrange(monkeypatch, tmp_path, *, owner="alice"):
    admission, session, _, options, uploader = configured(tmp_path, owner=owner)
    ports = PersistAttachmentPorts()
    monkeypatch.setattr(gateway_text_adapter, "LegacyChatContextPorts", lambda: ports)
    saved, notifications = [], []

    def saver(sess, manager, sid, text, metrics, **kw):
        assert sess is session and sid == session.id
        assert session.history[0].content == "Summarize the attached note."
        saved.append((text, metrics))
        session.history.append(ChatMessage(
            "assistant", text, metadata={"_db_id": "fixture-assistant-2"},
        ))

    gateway = GatewayTurns(TurnService())
    app = FastAPI()

    @app.post("/api/chat_stream")
    async def route():
        return gateway_text_adapter.start_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            text="Summarize the attached note.",
            attachment_ids=(ATTACHMENT_ID,),
            session_manager=object(),
            chat_handler=options["chat_handler"],
            chat_processor=options["chat_processor"],
            privileges={},
            assistant_writer=saver, interrupted_writer=None,
            after_user_saved=lambda: notifications.append("user"),
            post_response_runner=lambda *a, **kw: notifications.append("post"),
        )
    return session, uploader, ports, saved, notifications, gateway, admission, app


@pytest.mark.parametrize("form,body,parsed,expected", [
    ({}, None, [], ()),
    ({"attachments": '["note-123.txt"]'}, None, [ATTACHMENT_ID], (ATTACHMENT_ID,)),
    ({}, {"attachments": [ATTACHMENT_ID]}, [ATTACHMENT_ID], (ATTACHMENT_ID,)),
    ({"attachments": '["note-123.txt"]'}, {"attachments": [ATTACHMENT_ID]}, [ATTACHMENT_ID], None),
    ({"attachments": '["note-123.txt"]'}, None, [], None),
    ({"attachments": '["note-123.txt", 42]'}, None, [ATTACHMENT_ID, "42"], None),
    ({"attachments": '["note-123.txt", "note-123.txt"]'}, None, [ATTACHMENT_ID]*2, None),
    ({"attachments": '{"id":"note-123.txt"}'}, None, [], None),
    ({"attachments": '["../bad.txt"]'}, None, ["../bad.txt"], None),
    ({}, {"attachments": [42]}, ["42"], None),
])
def test_exact_strict_web_upload_selection(form, body, parsed, expected):
    assert parse_strict_web_text_attachment_ids(form, body, parsed) == expected
    assert eligible(attachments=expected) == (expected is not None)


@pytest.mark.asyncio
async def test_persisted_http_attachment_is_untrusted_context_and_one_write(
    monkeypatch, tmp_path,
):
    session, uploader, ports, saved, notifications, gateway, admission, app = arrange(
        monkeypatch, tmp_path,
    )
    prompts, policies = [], []

    async def provider(url, model, messages, **kwargs):
        prompts.append(list(messages))
        policies.append(kwargs["tool_policy"])
        yield 'data: {"delta":"The report is read only."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        run_id = response.headers["x-odysseus-run-id"]
        await agent_runs.get_matching_run(session.id, run_id, mode="typed").task
        assert response.status_code == 200
        assert response.text.count("data: [DONE]") == 1
        assert response.text.count('"type":"message_saved"') == 1
        assert len(prompts) == len(saved) == 1
        assert notifications == ["user", "post"]
        assert session.history[0].content == "Summarize the attached note."
        assert session.history[0].metadata["attachments"][0]["id"] == ATTACHMENT_ID
        assert all(CONTENT not in item.content for item in session.history)
        assert prompts[0][-1]["content"] == "Summarize the attached note."
        assert prompts[0][-2]["metadata"]["trusted"] is False
        assert prompts[0][-2]["metadata"]["tool_gate_untrusted"] is True
        assert CONTENT.splitlines()[0] in prompts[0][-2]["content"]
        assert policies[0].block_all_tool_calls
        assert ports.operations.count("save_user") == 1

        replay = gateway_text_adapter.resume_strict_text_web_response(
            gateway=gateway, admission=admission, session=session,
            expected_run_id=run_id, after_cursor=None,
        )
        assert replay is not None
        assert any('"type":"message_saved"' in frame
                   for frame in [f async for f in replay.body_iterator])
        assert len(saved) == len(prompts) == 1
        assert notifications == ["user", "post"]
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "foreign", "during", "oversized", "binary", "extension", "symlink",
])
async def test_rejected_upload_cannot_write_user_or_call_model(
    monkeypatch, tmp_path, mutation,
):
    session, uploader, ports, saved, notifications, gateway, admission, app = arrange(
        monkeypatch, tmp_path, owner="mallory" if mutation == "foreign" else "alice",
    )
    if mutation == "during":
        original = ports.preprocess_message
        async def changed(*args, **kwargs):
            value = await original(*args, **kwargs)
            uploader.path.write_text(CONTENT + " rewritten")
            return value
        monkeypatch.setattr(ports, "preprocess_message", changed)
    elif mutation == "oversized":
        uploader.path.write_text("x" * 65537)
    elif mutation == "binary":
        uploader.path.write_bytes(b"binary\x00file")
    elif mutation == "extension":
        uploader.name = "unsupported.pdf"
    elif mutation == "symlink":
        other = tmp_path / "outside.txt"
        other.write_text(CONTENT)
        uploader.path.unlink()
        uploader.path.symlink_to(other)

    invoked = []
    async def forbidden(*args, **kwargs):
        invoked.append(True)
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(runtime, "stream_agent_loop", forbidden)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://example.invalid",
        ) as client:
            response = await client.post("/api/chat_stream")
        assert response.status_code == 200
        assert not invoked and not saved and not notifications
        assert "save_user" not in ports.operations
        assert session.history == []
        assert session.messages == []
        assert "data: [DONE]" not in response.text
    finally:
        await cleanup_run(session.id)


@pytest.mark.asyncio
async def test_original_unselected_direct_persistence_remains_denied(
    tmp_path,
):
    from src.gateway_text_persistence import TrustedTextPersistence
    admission, session, _, options, uploader = configured(tmp_path)
    ports = PersistAttachmentPorts()
    options["ports"] = ports
    options["persistence"] = TrustedTextPersistence(object(), lambda *a, **k: None)
    stream = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    with pytest.raises(AdmissionDenied):
        _ = [event async for event in stream.events]
    assert ports.operations == [] and session.history == []



@pytest.mark.asyncio
@pytest.mark.parametrize("raw_ids,should_select", [
    ('["note-123.txt"]', True),
    ('["note-123.txt", 42]', False),
    ('["../unsafe.txt"]', False),
    ('["note-123.txt", "note-123.txt"]', False),
    ('not json', False),
])
async def test_real_existing_web_route_only_selects_exact_text_ids(
    monkeypatch, raw_ids, should_select,
):
    import routes.chat_routes as chat_routes
    from tests.test_foreground_model_routing import (
        _RouteRequest, _chat_stream_endpoint,
    )
    captures = {}
    route = _chat_stream_endpoint(
        monkeypatch, "chat", captures, capture_context=True,
    )
    monkeypatch.setenv("MNERYS_M21_WEB_TEXT_ROUTING", "1")
    request = _RouteRequest("chat")
    request._form = {
        "message": "hello",
        "session": "session-1",
        "mode": "chat",
        "gateway_profile": "text_v1",
        "no_memory": "true",
        "use_rag": "false",
        "active_doc_state": "none",
        "attachments": raw_ids,
    }
    routed = object()
    invoked = []

    def fake_new(**kwargs):
        invoked.append(kwargs)
        return routed

    monkeypatch.setattr(chat_routes, "start_strict_text_web_response", fake_new)
    reply = await route(request)
    if should_select:
        assert reply is routed
        assert len(invoked) == 1
        assert invoked[0]["attachment_ids"] == (ATTACHMENT_ID,)
        assert "build_context" not in captures
    else:
        assert not invoked
        # The inherited Web endpoint builds context before the SSE body is
        # consumed, then invokes the original model inside its iterator.
        assert "build_context" in captures
        _ = [frame async for frame in reply.body_iterator]
        # M5.3 increment 5: the fallback runs the UNIFIED agent-first path
        # (the legacy mode/profile value never selects a separate engine
        # once the strict compatibility route was not selected).
        assert "agent" in captures



@pytest.mark.asyncio
@pytest.mark.parametrize("with_opt_outs,selected", [
    (True, True),
    (False, False),
])
async def test_existing_json_http_profile_requires_explicit_no_memory_and_rag_flags(
    monkeypatch, with_opt_outs, selected,
):
    import routes.chat_routes as chat_routes
    from tests.test_foreground_model_routing import (
        _RouteRequest, _chat_stream_endpoint,
    )
    captured = {}
    endpoint = _chat_stream_endpoint(
        monkeypatch, "chat", captured, capture_context=True,
    )
    monkeypatch.setenv("MNERYS_M21_WEB_TEXT_ROUTING", "1")

    class JsonRequest(_RouteRequest):
        def __init__(self):
            super().__init__("chat")
            self._form = {}
            self.headers = {"content-type": "application/json"}
            self.payload = {
                "message": "hello",
                "session": "session-1",
                "gateway_profile": "text_v1",
                "mode": "chat",
                "active_doc_state": "none",
                "attachments": [ATTACHMENT_ID],
            }
            if with_opt_outs:
                self.payload.update({"no_memory": True, "use_rag": False})

        async def json(self):
            return self.payload

    invoked = []
    routed = object()
    def selected_path(**kwargs):
        invoked.append(kwargs)
        return routed

    monkeypatch.setattr(chat_routes, "start_strict_text_web_response", selected_path)
    selected_gate = chat_routes.eligible_strict_web_text_turn
    calls = []
    def observe_gate(**kwargs):
        success = selected_gate(**kwargs)
        calls.append((success, {
            key: kwargs[key] for key in (
                "chat_mode", "no_memory", "use_rag", "attachments",
                "image_generation_session", "agent_permitted",
                "active_doc_state", "thinking_mode", "reasoning_effort",
                "selected_endpoint_id", "approved_plan",
                "foreground_fallback_enabled", "workspace", "workspace_rejected",
                "incognito", "compare_mode", "plan_mode", "do_research",
                "tool_approval_continuation", "client_runtime_context",
            )
        }))
        return success
    monkeypatch.setattr(chat_routes, "eligible_strict_web_text_turn", observe_gate)
    result = await endpoint(JsonRequest())
    if selected:
        assert result is routed, calls
        assert len(invoked) == 1
        assert invoked[0]["attachment_ids"] == (ATTACHMENT_ID,)
        assert "build_context" not in captured
    else:
        assert not invoked
        assert "build_context" in captured
        _ = [frame async for frame in result.body_iterator]
        # M5.3 increment 5: the fallback runs the UNIFIED agent-first path
        # (the legacy mode value never selects the separate chat engine).
        assert "agent" in captured
