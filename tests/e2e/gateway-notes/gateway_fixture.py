"""Isolated integration fixture: the *real* Gateway Web route + synthetic model.

Guardrails:
* This is not app:app and never starts the actual service initializers.
* No real provider/tool effect, network connector or database is configured.
* Only binds 127.0.0.1 on a test-only port.
* HTTP auth is test-admitted from one synthetic local session only. This is
  deliberately NOT a production authentication acceptance test.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src import agent_runs, turn_service as runtime
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.tool_capabilities import capabilities_for_action
from tests.test_mnerys_gateway_web_native_notes_integration import route_fixture

FIXTURE_MARKER = "MNERYS_GATEWAY_NOTES_TEST_ONLY"


def create_test_app():
    monkeypatch = pytest.MonkeyPatch()
    admission, session, ports, writes, partials, store, gateway, endpoints = route_fixture(monkeypatch)

    # Synthetic HTTP security admission for a single test-owned session.
    # Original route admission and Core review policy still execute normally.
    import routes.chat_routes as routes
    import src.agent_runtime.authority as authority

    human_checks = []
    monkeypatch.setattr(routes, "require_api_token_scope", lambda *a, **kw: None)
    monkeypatch.setattr(
        authority, "require_user_approval_request",
        lambda req: human_checks.append("interactive"),
    )

    effects = []
    proposals = []

    async def scripted_model(_url, _model, _messages, **kwargs):
        if kwargs.get("exact_approval") is None:
            content = '{"action":"list"}'
            pending = store.create(
                owner="alice", session_id=session.id,
                origin_run_id="integration-browser-notes-fixture",
                tool_name="manage_notes", content=content, workspace=None,
                request_text="List my notes", continuation_query="List my notes",
                external_untrusted_context_seen=True,
                capabilities=capabilities_for_action("manage_notes", content),
                selected_tools={"manage_notes"},
                request_authority=kwargs["request_authority"],
            )
            proposals.append(pending)
            yield approval_requested_event(pending)
            yield tool_event_frame({
                "type": "tool_output", "tool": "manage_notes",
                "round": 1, "exit_code": None, "output": "FIXTURE_TOOL_PRIVATE",
            }, result={"ask_user": pending.public_payload()})
            yield 'data: {"type":"final_response","content":"Review required"}\n\n'
            yield "data: [DONE]\n\n"
            return

        approved = kwargs["exact_approval"]
        pending = approved.pending
        if not approved.claim(
            owner="alice", session_id=session.id,
            tool_name="manage_notes", content=pending.content,
            workspace=None,
        ):
            raise RuntimeError("Synthetic exact-action claim failed")
        effects.append("one")
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0, "output": "FIXTURE_TOOL_PRIVATE",
        })
        yield 'data: {"delta":"Browser Gateway integration complete."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", scripted_model)
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/login", response_class=HTMLResponse)
    async def browser_shell():
        return HTMLResponse("""<!doctype html><html lang="en">
        <head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1"></head>
        <body><main><div id="chat-history" role="log"></div>
        <textarea id="message"></textarea>
        <button class="send-btn">Send</button></main></body></html>""")

    @app.get("/__fixture/health")
    async def health():
        return {"fixture": FIXTURE_MARKER}

    @app.post("/__fixture/reset")
    async def reset():
        # Explicit test-only cleanup between independent browser tests.
        # Never retire an active run; there are no production sessions here.
        if agent_runs.get_active_run(session.id) is not None:
            raise HTTPException(409, "The test run has not finished")
        old_run = agent_runs._RUNS.pop(session.id, None)
        if old_run is not None and old_run.evict_task and not old_run.evict_task.done():
            old_run.evict_task.cancel()
        store.retire_for_session(owner="alice", session_id=session.id)
        # The fixture uses deterministic synthetic message IDs. Reset ONLY
        # disposable in-memory SQL rows between independent Playwright tests.
        # Production databases are never initialized by this harness.
        import routes.chat_routes as routes
        from src.gateway_notes_event_journal import notes_event_journal_metadata
        from src.gateway_notes_decision_fence import notes_fence_metadata
        db = routes.server_notes_event_journal()._engine
        if (
            db.dialect.name != "sqlite"
            or db.url.database not in (None, "", ":memory:")
        ):
            raise HTTPException(500, "Refusing to reset a non-disposable test database")
        with db.begin() as conn:
            conn.execute(
                notes_event_journal_metadata().tables[
                    "gateway_notes_event_journal"
                ].delete()
            )
            conn.execute(
                notes_fence_metadata().tables[
                    "gateway_notes_decision_fences"
                ].delete()
            )
        session.messages.clear()
        session.history.clear()
        session.notes_route_events.clear()
        session.notes_route_post_jobs.clear()
        ports.operations.clear()
        writes.clear()
        partials.clear()
        proposals.clear()
        effects.clear()
        human_checks.clear()
        return {"reset": True}

    @app.post("/__fixture/forget-run")
    async def forget_run():
        # Simulate a process-local run registry disappearing while the
        # separate SQL metadata-only ledger survives. No real process,
        # database or permission state is restarted or recovered here.
        run = agent_runs._RUNS.get(session.id)
        if run is None or run.status == "running":
            raise HTTPException(409, "No finished run to forget")
        agent_runs._RUNS.pop(session.id, None)
        if run.evict_task and not run.evict_task.done():
            run.evict_task.cancel()
        return {"forgotten": True}

    @app.get("/__fixture/state")
    async def state():
        # Metadata counts only; never expose pending tools' private contents.
        return {
            "user_messages": len([m for m in session.history if m.role == "user"]),
            "assistant_messages": len([m for m in session.history if m.role == "assistant"]),
            "proposals": len(proposals),
            "effects": len(effects),
            "writes": len(writes),
            "post_response_jobs": len(session.notes_route_post_jobs),
            "human_checks": len(human_checks),
            "pending": sum(store.peek(r.approval_id) is not None for r in proposals),
        }

    for method, path in (
        ("POST", "/api/chat_stream"),
        ("GET", "/api/chat/resume/{session_id}"),
        ("GET", "/api/chat/stream_status/{session_id}"),
        ("GET", "/api/chat/notes_journal/{session_id}"),
        ("POST", "/api/chat/stop/{session_id}"),
    ):
        app.add_api_route(
            path, endpoints[path], methods=[method],
            response_class=JSONResponse,
        )

    app.state.fixture_monkeypatch = monkeypatch
    return app


if __name__ == "__main__":
    if os.environ.get(FIXTURE_MARKER) != "1":
        raise SystemExit("Refusing to run the integration fixture without test opt-in")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7015)
    args = parser.parse_args()
    if not (1024 <= args.port <= 65535):
        raise SystemExit("Invalid integration test port")
    uvicorn.run(
        create_test_app(), host="127.0.0.1", port=args.port,
        access_log=False, log_level="error",
    )
