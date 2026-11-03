"""M2.1 HTTP-independent chat-context preparation and admitted owner checks.

The legacy FastAPI adapter may still be called by existing tests. Real Web
routes pass issued admission; this suite verifies mismatch denial before any
message, memory, RAG or persistence side effect.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from routes import chat_helpers
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
)
from src.gateway_context_admission import owner_for_admitted_context
from src.session_repository import SessionOwnership


def _admit(owner="alice", session_id="session-a", kind="browser"):
    return GatewayAdmission().admit(
        Principal(
            owner, kind,
            frozenset({"chat"}) if kind == "bearer" else frozenset(),
        ),
        session_id,
        lambda _sid: SessionOwnership(owner),
    )


def _session(owner="alice", session_id="session-a"):
    session = SimpleNamespace(
        id=session_id,
        owner=owner,
        endpoint_url="https://example.com/v1",
        model="selected-model",
        headers={},
        history=[],
        messages=[],
    )
    session.get_context_messages = lambda: list(session.messages)
    return session


def _adapter_request(owner="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=owner))


def _configure_core(monkeypatch):
    seen = {"owners": [], "events": []}

    async def fake_preprocess(_handler, message, _att, _session, **_kw):
        return chat_helpers.PreprocessedMessage(
            enhanced_message=message,
            user_content=message,
            text_for_context=message,
            youtube_transcripts=[],
            attachment_meta=[],
        )

    def fake_append(session, _handler, preprocessed, **_kw):
        seen["events"].append("append")
        session.messages.append({"role": "user", "content": preprocessed.user_content})

    def fake_prefs(owner):
        seen["owners"].append(("prefs", owner))
        return {"memory_enabled": False, "skills_enabled": False}

    def fake_preface(**kwargs):
        seen["owners"].append(("preface", kwargs["owner"]))
        return [], [], []

    async def fake_compact(_sess, _url, _model, messages, _headers, **kwargs):
        seen["owners"].append(("compact", kwargs["owner"]))
        return messages, 8192, False

    monkeypatch.setattr(chat_helpers, "preprocess", fake_preprocess)
    monkeypatch.setattr(
        chat_helpers, "extract_preset",
        lambda *_args: SimpleNamespace(
            system_prompt=None, character_name=None, persona_memory=None,
        ),
    )
    monkeypatch.setattr(chat_helpers, "add_user_message", fake_append)
    monkeypatch.setattr(chat_helpers, "load_prefs_for_user", fake_prefs)
    monkeypatch.setattr(chat_helpers, "build_uploaded_file_manifest", lambda *_: [])
    monkeypatch.setattr(chat_helpers, "_normalize_model_id_from_cache", lambda _: None)
    monkeypatch.setattr(chat_helpers, "normalize_model_id", lambda *_a, **_kw: None)
    monkeypatch.setattr(chat_helpers, "maybe_compact", fake_compact)
    monkeypatch.setattr(chat_helpers, "trim_for_context", lambda msgs, _limit: msgs)
    return seen, SimpleNamespace(build_context_preface=fake_preface)


@pytest.mark.parametrize("bad", ["missing", "forged", "id", "principal", "proof"])
def test_context_ownership_rejects_unissued_or_modified_admission(bad):
    valid = _admit()
    invalid = {
        "missing": None,
        "forged": AdmittedSession(valid.principal, valid.session_id),
        "id": replace(valid, session_id="session-b"),
        "principal": replace(valid, principal=Principal("mallory", "browser")),
        "proof": replace(valid, _proof=object()),
    }[bad]
    with pytest.raises(AdmissionDenied) as exc:
        owner_for_admitted_context(invalid, _session(), "session-a")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("kind", ["request_id", "session_id", "owner"])
def test_mismatched_context_session_fails_closed(kind):
    admission = _admit()
    sess = _session()
    requested = "session-a"
    if kind == "request_id":
        requested = "session-b"
    elif kind == "session_id":
        sess.id = "session-b"
    else:
        sess.owner = "mallory"
    with pytest.raises(AdmissionDenied) as exc:
        owner_for_admitted_context(admission, sess, requested)
    assert exc.value.status_code == 403


def test_legacy_missing_cached_owner_and_single_user_are_supported():
    assert owner_for_admitted_context(
        _admit(), _session(owner=None), "session-a",
    ) == "alice"
    assert owner_for_admitted_context(
        _admit(owner=None, kind="single_user"),
        _session(owner=None), "session-a",
    ) is None


@pytest.mark.asyncio
async def test_core_prepares_owner_scoped_context_without_http_request(monkeypatch):
    seen, processor = _configure_core(monkeypatch)
    sess = _session()
    result = await chat_helpers.build_chat_context_core(
        sess, "alice", SimpleNamespace(), processor,
        "hello", "session-a", agent_mode=True,
        on_user_message_persisted=lambda: seen["events"].append("event"),
    )
    assert result.user == "alice"
    assert seen["events"] == ["append", "event"]
    assert seen["owners"] == [
        ("prefs", "alice"), ("preface", "alice"), ("compact", "alice"),
    ]
    assert result.messages[-1] == {"role": "user", "content": "hello"}


@pytest.mark.asyncio
async def test_no_persistence_or_user_event_on_internal_continuation(monkeypatch):
    seen, processor = _configure_core(monkeypatch)
    sess = _session()
    result = await chat_helpers.build_chat_context_core(
        sess, "alice", SimpleNamespace(), processor,
        "continue", "session-a",
        persist_user_message=False,
        agent_mode=True,
        on_user_message_persisted=lambda: seen["events"].append("event"),
    )
    assert result.user == "alice"
    assert seen["events"] == []
    assert sess.messages == []


@pytest.mark.asyncio
async def test_http_adapter_uses_issued_identity_for_full_context(monkeypatch):
    seen, processor = _configure_core(monkeypatch)
    sess = _session()
    published = []

    monkeypatch.setattr(
        chat_helpers, "fire_message_event",
        lambda *args: published.append((args[2], args[4])),
    )
    result = await chat_helpers.build_chat_context(
        sess, _adapter_request("alice"), SimpleNamespace(), processor,
        "hello", "session-a", agent_mode=True, admission=_admit(),
    )
    assert result.user == "alice"
    assert seen["owners"] == [
        ("prefs", "alice"), ("preface", "alice"), ("compact", "alice"),
    ]
    assert seen["events"] == ["append"]
    assert published == [("session-a", "hello")]


@pytest.mark.asyncio
@pytest.mark.parametrize("different", [
    "request_owner",
    "session_owner",
    "session_id",
    "forged",
])
async def test_http_context_guard_rejects_mismatch_before_preprocess_or_write(
    monkeypatch, different,
):
    seen, processor = _configure_core(monkeypatch)
    sess = _session()
    grant = _admit()
    request = _adapter_request("alice")
    if different == "request_owner":
        request = _adapter_request("bob")
    elif different == "session_owner":
        sess.owner = "bob"
    elif different == "session_id":
        sess.id = "session-b"
    else:
        grant = AdmittedSession(grant.principal, grant.session_id)

    monkeypatch.setattr(
        chat_helpers, "preprocess",
        lambda *_a, **_kw: pytest.fail("Invalid turn reached preprocessing"),
    )
    with pytest.raises(AdmissionDenied) as exc:
        await chat_helpers.build_chat_context(
            sess, request, SimpleNamespace(), processor,
            "hello", "session-a", admission=grant,
        )
    assert exc.value.status_code == 403
    assert seen["events"] == []
    assert seen["owners"] == []
    assert sess.messages == []


@pytest.mark.asyncio
async def test_non_admitted_legacy_http_adapter_preserves_existing_owner(monkeypatch):
    seen, processor = _configure_core(monkeypatch)
    sess = _session(owner="bob")
    result = await chat_helpers.build_chat_context(
        sess, _adapter_request("bob"), SimpleNamespace(), processor,
        "hi", "session-a", agent_mode=True,
        persist_user_message=False,
    )
    assert result.user == "bob"
    assert seen["owners"] == [
        ("prefs", "bob"), ("preface", "bob"), ("compact", "bob"),
    ]


def test_shared_context_core_has_no_http_or_webhook_parameters():
    from inspect import signature

    names = signature(chat_helpers.build_chat_context_core).parameters
    assert "user" in names
    assert "on_user_message_persisted" in names
    assert "request" not in names
    assert "webhook_manager" not in names
