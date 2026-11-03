"""M2.1 context ports: prove optional I/O providers actually replace legacy hooks.

No database, model endpoint, browser or file service is used by these tests.
"""

from types import SimpleNamespace
import pytest

from routes import chat_helpers
from src.gateway_context_ports import ChatContextPorts


class RecordingContextPorts:
    """A complete in-memory provider that does not call inherited services."""

    def __init__(self):
        self.events = []
        self.private = {}
        self.preface_options = {}

    def extract_preset(self, handler, preset_id):
        self.events.append("preset")
        return SimpleNamespace(
            system_prompt=None, character_name=None, persona_memory=None,
        )

    async def preprocess_message(
        self, handler, message, attachment_ids, session, *,
        auto_opened_docs, allow_tool_preprocessing,
    ):
        self.events.append("preprocess")
        return chat_helpers.PreprocessedMessage(
            enhanced_message=message,
            user_content=message,
            text_for_context=message,
            youtube_transcripts=[],
            attachment_meta=[],
        )

    def append_user_message(
        self, session, handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        self.events.append("persisted_user")
        session.messages.append({
            "role": "user", "content": preprocessed.user_content,
        })

    def append_private_message(self, session_id, role, content, metadata):
        self.events.append("private_user")
        self.private.setdefault(session_id, []).append({
            "role": role, "content": content, "metadata": metadata,
        })

    def private_messages(self, session_id):
        self.events.append("private_read")
        return list(self.private.get(session_id, []))

    def load_preferences(self, owner):
        self.events.append(("preferences", owner))
        return {"memory_enabled": True, "skills_enabled": True}

    def upload_manifest(self, attachment_ids, upload_handler, owner):
        self.events.append(("manifest", tuple(attachment_ids), owner))
        return [{"attachment_id": item} for item in attachment_ids]

    def build_preface(self, processor, **options):
        self.events.append("preface")
        self.preface_options = options
        return ([{"role": "system", "content": "selected-preamble"}], [], [])

    def used_memories(self, processor):
        self.events.append("used_memories")
        return [{"kind": "in-memory-fixture"}]

    async def compact(
        self, session, endpoint_url, model, messages, headers, *,
        owner, **options,
    ):
        self.events.append(("compact", owner))
        return messages, 16384, False

    def trim(self, messages, context_length):
        self.events.append(("trim", context_length))
        return messages

    def youtube_sources(self, message, transcripts):
        self.events.append("youtube_sources")
        return []

    def normalize_model(self, session):
        self.events.append("normalize_model")
        return None

    def session_history(self, session):
        self.events.append("session_history")
        return list(session.messages)

    def current_datetime_message(self):
        self.events.append("current_datetime")
        return {"role": "user", "content": "fixed clock context"}

    def context_length(self, endpoint_url, model):
        self.events.append("context_length")
        return 32768

    def estimate_tokens(self, messages):
        self.events.append("estimate_tokens")
        return len(messages) * 10


def test_legacy_normalizer_prefers_cached_model_without_live_probe(monkeypatch):
    monkeypatch.setattr(
        chat_helpers, "_normalize_model_id_from_cache",
        lambda _session: "provider/cached-model",
    )
    monkeypatch.setattr(
        chat_helpers, "normalize_model_id",
        lambda *_a, **_kw: pytest.fail("Cached model unexpectedly probed provider"),
    )
    assert chat_helpers.LegacyChatContextPorts().normalize_model(_session()) == (
        "provider/cached-model"
    )


def test_legacy_normalizer_preserves_owner_scoped_fallback(monkeypatch):
    monkeypatch.setattr(
        chat_helpers, "_normalize_model_id_from_cache",
        lambda _session: None,
    )
    checked = []
    def fake_resolve(url, model, *, owner):
        checked.append((url, model, owner))
        return "provider/normalized-model"

    monkeypatch.setattr(chat_helpers, "normalize_model_id", fake_resolve)
    assert chat_helpers.LegacyChatContextPorts().normalize_model(_session()) == (
        "provider/normalized-model"
    )
    assert checked == [
        ("https://example.com/v1", "selected-model", "alice")
    ]


def _session():
    sess = SimpleNamespace(
        id="session-a", owner="alice",
        endpoint_url="https://example.com/v1",
        model="selected-model",
        headers={}, history=[], messages=[],
    )
    sess.get_context_messages = lambda: list(sess.messages)
    return sess


def _block_all_legacy(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Context core called an inherited service directly")

    async def forbidden_async(*_args, **_kwargs):
        pytest.fail("Context core called an inherited async service directly")

    for name in (
        "extract_preset", "add_user_message", "load_prefs_for_user",
        "build_uploaded_file_manifest", "_append_incognito_message",
        "_incognito_messages", "trim_for_context",
        "youtube_prefetch_sources", "_normalize_model_id_from_cache",
        "normalize_model_id", "get_context_length", "estimate_tokens",
    ):
        monkeypatch.setattr(chat_helpers, name, forbidden)
    for name in ("preprocess", "maybe_compact"):
        monkeypatch.setattr(chat_helpers, name, forbidden_async)
    from src import user_time
    monkeypatch.setattr(user_time, "current_datetime_context_message", forbidden)


@pytest.mark.asyncio
async def test_injected_ports_replace_all_shared_context_io_without_http(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    sess = _session()
    processors = SimpleNamespace(
        build_context_preface=lambda **_kw: pytest.fail("Legacy RAG called"),
    )
    service_ports: ChatContextPorts = ports
    event = []
    result = await chat_helpers.build_chat_context_core(
        sess, "alice", SimpleNamespace(), processors,
        "My request", "session-a", agent_mode=True,
        att_ids=["attachment-1"],
        ports=service_ports,
        on_user_message_persisted=lambda: event.append("sent"),
    )
    assert event == ["sent"]
    assert result.user == "alice"
    assert result.uploaded_files == [{"attachment_id": "attachment-1"}]
    assert result.used_memories == [{"kind": "in-memory-fixture"}]
    assert result.messages == [
        {"role": "system", "content": "selected-preamble"},
        {"role": "user", "content": "My request"},
    ]
    assert ("preferences", "alice") in ports.events
    assert ("manifest", ("attachment-1",), "alice") in ports.events
    assert ("compact", "alice") in ports.events
    assert ports.events.index("persisted_user") < ports.events.index(("preferences", "alice"))
    assert ports.events.index("preface") < ports.events.index(("compact", "alice"))
    assert "normalize_model" in ports.events
    assert "session_history" in ports.events
    assert "youtube_sources" in ports.events
    assert ports.events.count("estimate_tokens") == 2


@pytest.mark.asyncio
async def test_history_and_current_clock_come_only_from_injected_ports(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    sess = _session()
    sess.get_context_messages = lambda: pytest.fail("Context bypassed injected history port")
    result = await chat_helpers.build_chat_context_core(
        sess, "alice", SimpleNamespace(), SimpleNamespace(),
        "Hello", "session-a", ports=ports, agent_mode=False,
    )
    assert result.messages[-2:] == [
        {"role": "user", "content": "fixed clock context"},
        {"role": "user", "content": "Hello"},
    ]
    assert ports.events.count("current_datetime") == 1
    assert ports.events.count("session_history") == 1
    assert ports.events.index("session_history") < ports.events.index("current_datetime")


@pytest.mark.asyncio
async def test_deferred_context_window_reads_injected_metadata_without_compaction(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    ctx = await chat_helpers.build_chat_context_core(
        _session(), "alice", SimpleNamespace(), SimpleNamespace(),
        "Hello", "session-a", ports=ports,
        agent_mode=True, defer_context_shaping=True,
    )
    assert ctx.context_length == 32768
    assert ctx.was_compacted is False
    assert "context_length" in ports.events
    assert not any(isinstance(item, tuple) and item[0] == "compact" for item in ports.events)
    assert not any(isinstance(item, tuple) and item[0] == "trim" for item in ports.events)


@pytest.mark.asyncio
async def test_youtube_sources_are_supplied_by_provider_without_live_fetch(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    source = {"url": "https://www.youtube.com/watch?v=example", "title": "Reference"}
    ports.youtube_sources = lambda message, transcripts: [source]
    result = await chat_helpers.build_chat_context_core(
        _session(), "alice", SimpleNamespace(), SimpleNamespace(),
        "Summarize this clip", "session-a", ports=ports, agent_mode=True,
    )
    assert source in result.web_sources


@pytest.mark.asyncio
async def test_incognito_port_isolation_preserves_private_history(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    session = _session()
    session.messages = [{"role": "user", "content": "saved private history"}]
    session.get_context_messages = lambda: pytest.fail("Incognito read persisted history")
    events = []
    result = await chat_helpers.build_chat_context_core(
        session, "alice", SimpleNamespace(), SimpleNamespace(),
        "anonymous request", "session-a",
        agent_mode=True,
        incognito=True,
        ports=ports,
        on_user_message_persisted=lambda: events.append("sent"),
    )
    assert events == []
    assert "persisted_user" not in ports.events
    assert "private_user" in ports.events
    assert "private_read" in ports.events
    assert result.messages[-1]["content"] == "anonymous request"
    assert all("saved private history" not in str(m) for m in result.messages)
    assert ports.preface_options["use_memory"] is False
    assert ports.preface_options["use_skills"] is False
    assert ports.preface_options["incognito"] is True


@pytest.mark.asyncio
async def test_tool_continuation_does_not_call_either_message_writer(monkeypatch):
    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    event = []
    result = await chat_helpers.build_chat_context_core(
        _session(), "alice", SimpleNamespace(), SimpleNamespace(),
        "continue", "session-a", agent_mode=True,
        persist_user_message=False,
        ports=ports,
        on_user_message_persisted=lambda: event.append("sent"),
    )
    assert result.user == "alice"
    assert event == []
    assert "persisted_user" not in ports.events
    assert "private_user" not in ports.events


@pytest.mark.asyncio
async def test_shared_src_context_core_runs_without_http_or_legacy_services(monkeypatch):
    """New application layer can build context directly using trusted ports."""
    from src.gateway_chat_context import assemble_chat_context
    from src.gateway_chat_context_types import ChatContext

    _block_all_legacy(monkeypatch)
    ports = RecordingContextPorts()
    context = await assemble_chat_context(
        _session(), "alice", SimpleNamespace(), SimpleNamespace(),
        "Direct transport-neutral turn", "session-a", agent_mode=True,
        ports=ports,
    )
    assert isinstance(context, ChatContext)
    assert isinstance(context, chat_helpers.ChatContext)
    assert context.user == "alice"
    assert context.messages[-1]["content"] == "Direct transport-neutral turn"
    assert "persisted_user" in ports.events


@pytest.mark.asyncio
async def test_shared_src_context_core_rejects_missing_trusted_ports():
    from src.gateway_chat_context import assemble_chat_context

    with pytest.raises(ValueError, match="Trusted ChatContextPorts"):
        await assemble_chat_context(
            _session(), "alice", SimpleNamespace(), SimpleNamespace(),
            "No adapter", "session-a",
        )


def test_extracted_context_modules_do_not_import_web_routes():
    """Catch accidental reintroduction of route-layer imports into the Core."""
    import ast
    import inspect
    from src import gateway_chat_context, gateway_chat_context_types
    from src import gateway_chat_context_rules

    for module in (gateway_chat_context, gateway_chat_context_types,
                   gateway_chat_context_rules):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("routes", "fastapi"))
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith(("routes", "fastapi"))
                           for alias in node.names)


def test_pure_context_rules_reexported_for_existing_chat_callers():
    from src import gateway_chat_context_rules

    assert chat_helpers._is_casual_low_signal is gateway_chat_context_rules._is_casual_low_signal
    assert chat_helpers._truthy_request_flag is gateway_chat_context_rules._truthy_request_flag
    assert chat_helpers._session_is_research_spinoff is gateway_chat_context_rules._session_is_research_spinoff
    assert gateway_chat_context_rules._is_casual_low_signal("hey") is True
    assert gateway_chat_context_rules._is_casual_low_signal("hey search email") is False
