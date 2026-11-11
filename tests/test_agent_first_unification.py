"""M5.3 increment 5: Agent-first experience and Chat/Agent unification.

ONE interactive execution path: an agent-permitted caller always runs the
agent-first loop — the historical ``mode=chat`` wire value is accepted for
compatibility but never selects a separate engine and never changes
authorization. A caller whose privileges demote the turn keeps the text-only
ceiling (an Agent label alone grants nothing; a Chat label alone grants
nothing). Historical sessions/transcripts stay readable; no destructive
migration.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.gateway_admission import GatewayAdmission, Principal, SessionOwnership
from src.gateway_execution_policy import (
    finalize_admitted_turn_tool_policy,
    resolve_unified_chat_mode,
)

OWNER = "alice"


def issued(kind="browser", *, owner=OWNER, session_id="example-session"):
    principal = Principal(
        owner, kind, frozenset({"chat"}) if kind == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, session_id, lambda _: SessionOwnership(owner_id=owner),
    )


def _finalize(chat_mode="agent", privileges=None, session=None, **kwargs):
    admission = issued()
    session = session or SimpleNamespace(
        id=admission.session_id, owner=OWNER, name="Chat",
    )
    # Mirror the route: the RAW mode is normalized once (agent-first) before
    # the admitted policy applies its demotion ceiling.
    allow_agent = not privileges or bool(privileges.get("can_use_agent", True))
    chat_mode = resolve_unified_chat_mode(chat_mode, allow_agent=allow_agent)
    return finalize_admitted_turn_tool_policy(
        admission, session,
        context_owner=OWNER,
        base_disabled_tools=kwargs.get("base_disabled_tools", frozenset()),
        privileges=privileges,
        global_disabled_tools=kwargs.get("global_disabled_tools", frozenset()),
        browser_tools=frozenset({"mcp__browser__open", "mcp__browser__click"}),
        last_user_message=kwargs.get("last_user_message", "Inspect this."),
        chat_mode=chat_mode,
        auto_escalated=False,
        workspace_agent_intent=False,
        use_turn_contract=kwargs.get("use_turn_contract", False),
        allow_browser_for_web_turn=False,
        compare_mode=False,
        plan_mode=False,
    )


# ── The unified mode normalization ────────────────────────────────────────

def test_legacy_chat_mode_normalizes_to_the_agent_path():
    assert resolve_unified_chat_mode("chat", allow_agent=True) == "agent"


def test_empty_and_unknown_modes_normalize_to_agent():
    assert resolve_unified_chat_mode("", allow_agent=True) == "agent"
    assert resolve_unified_chat_mode("voice", allow_agent=True) == "agent"
    assert resolve_unified_chat_mode(None, allow_agent=True) == "agent"


def test_demoted_callers_keep_the_text_only_ceiling():
    assert resolve_unified_chat_mode("agent", allow_agent=False) == "chat"
    assert resolve_unified_chat_mode("chat", allow_agent=False) == "chat"


def test_final_policy_keeps_the_demotion():
    policy = _finalize(chat_mode="agent", privileges={"can_use_agent": False})
    assert policy.chat_mode == "chat"
    policy = _finalize(chat_mode="chat", privileges={"can_use_agent": True})
    assert policy.chat_mode == "agent"


# ── Legacy compatibility: the chat-only clamp for demoted callers ─────────

def test_demoted_caller_loses_execution_tools_in_chat_mode():
    policy = _finalize(chat_mode="agent", privileges={"can_use_agent": False})
    # The text-only ceiling is the EXECUTION PATH itself: a demoted caller's
    # final mode is 'chat' and the chat adapter streams with tools=None (the
    # inherited compatibility adapter — audited in the route source).
    assert policy.chat_mode == "chat"
    assert policy.allow_agent is False
    import inspect

    from routes import chat_routes

    source = inspect.getsource(chat_routes)
    assert "Chat mode: call stream_llm directly, NO tools, NO document access" in source
    assert "tools=None," in source
    policy_chat = _finalize(chat_mode="chat", privileges={"can_use_agent": False})
    assert policy_chat.chat_mode == "chat"


def test_authorized_agent_first_turn_offers_the_canonical_surface():
    policy = _finalize(chat_mode="chat", privileges={"can_use_agent": True})
    assert policy.chat_mode == "agent"
    # Nothing was silently denied by the legacy chat label.
    for handler in ("bash", "read_file", "write_file"):
        assert not policy.policy.blocks(handler)


def test_explicit_policy_denials_survive_the_unification():
    policy = _finalize(
        chat_mode="agent",
        privileges={"can_use_agent": True},
        base_disabled_tools=frozenset({"bash"}),
    )
    assert policy.policy.blocks("bash")
    assert not policy.policy.blocks("read_file")


# ── The web UI: the toggle is gone, features remain ───────────────────────

def test_composer_html_no_longer_renders_the_mode_toggle():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert 'id="mode-agent-btn"' not in html
    assert 'id="mode-chat-btn"' not in html
    # The unified composer keeps its real controls.
    assert 'id="web-toggle-btn"' in html
    assert 'id="bash-toggle-btn"' in html


def test_frontend_sources_no_longer_drive_the_toggle():
    for name in ("chat.js", "chatStream.js", "document.js"):
        text = (Path("static/js") / name).read_text(encoding="utf-8")
        assert "mode-agent-btn" not in text, name
        assert "mode-chat-btn" not in text, name
    chat = (Path("static/js") / "chat.js").read_text(encoding="utf-8")
    assert "fd.append('mode', 'agent')" in chat
    assert "fd.append('mode', isAgentMode ? 'agent' : 'chat')" not in chat
    assert "fd.set('mode', 'chat')" not in chat


def test_shared_mode_toggle_css_survives_for_other_panels():
    # The cookbook/serve panels legitimately reuse the .mode-toggle styles;
    # the unification must not break them.
    css_candidates = list(Path("static/css").glob("*.css")) if Path("static/css").exists() else []
    hits = [
        p.name for p in css_candidates
        if ".mode-toggle" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits  # styles untouched


def test_historical_mode_values_stay_readable():
    # Historical transcripts carry user-mode metadata; the renderer keeps
    # showing the recorded pill for old bubbles (compat, no migration).
    renderer = (Path("static/js") / "chatRenderer.js").read_text(encoding="utf-8")
    assert "user-mode-pill" in renderer


# ── Event compatibility ───────────────────────────────────────────────────

def test_turn_mode_event_still_emitted_for_streaming_clients():
    # The chat stream's `turn_mode` SSE event is part of the legacy event
    # contract; the unified path stamps it with the resolved mode.
    import inspect

    from routes import chat_routes

    source = inspect.getsource(chat_routes)
    assert "'turn_mode'" in source
