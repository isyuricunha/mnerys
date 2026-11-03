"""M2.1 per-model context shaping under an admitted owner/session.

Tests do not call any remote provider or write persistent session data.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import gateway_foreground_context as context
from src import foreground_model_routing as routing
from src.foreground_model_routing import ForegroundModelPolicy
from src.gateway_admission import (
    AdmittedSession, AdmissionDenied, GatewayAdmission, Principal,
)
from src.gateway_foreground_routes import AdmittedForegroundPolicy
from src.gateway_turns import GatewayTurns
from src.session_repository import SessionOwnership


def _admit(owner="alice", session_id="thread-a"):
    return GatewayAdmission().admit(
        Principal(owner, "browser"),
        session_id,
        lambda _: SessionOwnership(owner),
    )


def _session(session_id="thread-a"):
    return SimpleNamespace(id=session_id)


@pytest.mark.asyncio
async def test_candidate_prompts_are_independent_and_do_not_persist(monkeypatch):
    policy = ForegroundModelPolicy(enabled=True)
    monkeypatch.setattr(routing, "resolve_foreground_model_policy", lambda **_: policy)
    captured = []
    async def fake_compact(session, url, model, messages, headers,
                           *, owner, persist, compaction_state):
        assert session.id == "thread-a"
        assert owner == "alice"
        assert persist is False
        captured.append((model, list(messages), dict(headers)))
        messages.append({"role": "system", "content": model})
        compaction_state["candidate"] = model
        return messages, (100 if model == "primary" else 1000), True

    monkeypatch.setattr(context, "maybe_compact", fake_compact)
    monkeypatch.setattr(
        context, "trim_for_context",
        lambda messages, length: messages[-1:] if length == 100 else messages,
    )
    monkeypatch.setattr(context, "estimate_tokens", lambda messages: len(messages) * 10)

    gate = GatewayTurns(Mock())
    admitted = _admit()
    prepared = gate.resolve_foreground_policy(admitted, allowed_models=None)
    neutral = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
    ]
    factory, state = gate.build_candidate_context_factory(
        admitted, prepared, neutral, 512, session=_session(),
    )
    original = await factory(0, "https://primary.example.com/v1", "primary", {})
    fallback = await factory(1, "https://fallback.example.com/v1", "backup", {})
    assert original["messages"] == [{"role": "system", "content": "primary"}]
    assert len(fallback["messages"]) == 3
    assert [model for model, _messages, _headers in captured] == ["primary", "backup"]
    assert all(rows == neutral for _model, rows, _headers in captured)
    assert neutral == [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
    ]
    assert state["context_lengths"] == {0: 100, 1: 1000}
    assert state["compactions"][0] == {"candidate": "primary"}
    assert state["compactions"][1] == {"candidate": "backup"}
    assert state["was_compacted"] == {0: True, 1: True}
    assert state["trim_stats"][0]["messages_before"] == 2
    assert state["trim_stats"][0]["messages_after"] == 1


@pytest.mark.asyncio
async def test_missing_candidate_context_length_uses_existing_fallback(monkeypatch):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(enabled=True),
    )
    async def compact(_session, _url, _model, messages, _headers, **_kwargs):
        return list(messages), 0, False

    seen = []
    monkeypatch.setattr(context, "maybe_compact", compact)
    monkeypatch.setattr(
        context, "trim_for_context",
        lambda messages, length: seen.append(length) or messages,
    )
    gate = GatewayTurns(Mock())
    admission = _admit()
    prepared = gate.resolve_foreground_policy(admission, allowed_models=None)
    factory, state = gate.build_candidate_context_factory(
        admission, prepared,
        [{"role": "user", "content": "hello"}], 8192,
        session=_session(),
    )
    result = await factory(0, "https://primary.example.com/v1", "primary", {})
    assert result["messages"][0]["content"] == "hello"
    assert seen == [8192]
    assert state["context_lengths"][0] == 8192
    assert state["was_compacted"][0] is False


@pytest.mark.parametrize("wrong_session", ["other", None])
def test_mismatched_session_fails_before_any_compactor_execution(
    monkeypatch, wrong_session,
):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(enabled=True),
    )
    monkeypatch.setattr(
        context, "maybe_compact",
        lambda *_a, **_kw: pytest.fail("Invalid session reached compactor"),
    )
    gate = GatewayTurns(Mock())
    admitted = _admit()
    prepared = gate.resolve_foreground_policy(admitted, allowed_models=None)
    with pytest.raises(AdmissionDenied) as error:
        gate.build_candidate_context_factory(
            admitted, prepared, [], session=_session(wrong_session),
        )
    assert error.value.status_code == 403


def test_strict_mode_does_not_prepare_fallback_compaction(monkeypatch):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(),
    )
    gate = GatewayTurns(Mock())
    admission = _admit()
    prepared = gate.resolve_foreground_policy(admission, allowed_models=None)
    with pytest.raises(AdmissionDenied):
        gate.build_candidate_context_factory(
            admission, prepared, [], session=_session(),
        )


@pytest.mark.parametrize("case", ["forged", "different_session", "different_owner", "foreign_policy"])
def test_context_factory_refuses_unadmitted_or_foreign_policy(monkeypatch, case):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(enabled=True),
    )
    gate = GatewayTurns(Mock())
    original = _admit()
    prepared = gate.resolve_foreground_policy(original, allowed_models=None)
    foreign = {
        "forged": AdmittedSession(original.principal, original.session_id),
        "different_session": _admit(session_id="thread-b"),
        "different_owner": _admit(owner="bob"),
        "foreign_policy": original,
    }[case]
    if case == "foreign_policy":
        prepared = AdmittedForegroundPolicy(original, ForegroundModelPolicy(enabled=True))
    with pytest.raises(AdmissionDenied) as error:
        gate.build_candidate_context_factory(
            foreign, prepared, [], session=_session(),
        )
    assert error.value.status_code == 403
