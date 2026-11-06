"""M2.4 versioned public Gateway API (v1).

The single authenticated control plane: sessions, turns, events,
approvals, cancellation and capability discovery, all owner-scoped and
server-enforced. This router never invokes the Agent Loop or writes the
session database directly; it enters exclusively through the existing
Gateway facade (GatewayTurns), the detached run manager and the durable
turn records. Capability discovery never grants execution rights.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routes.gateway_principal import principal_for_chat_request
from src.gateway_admission import (
    AdmissionDenied, GatewayAdmission, SessionOwnership,
)
from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turn_records import TurnRecordError
from src.gateway_turns import GatewayTurns

logger = logging.getLogger("mnerys.gateway_api")


async def _finalize_on_terminal(
    events: AsyncIterator[Any], turn_id: str, owner: str, records: Any,
) -> AsyncIterator[Any]:
    """Forward events and finalize the durable record on the terminal state.

    The detached run owns the in-process lifecycle; the durable record
    mirrors only the authentic terminal event seen on the stream, never the
    mutable outcome snapshot. Finalization is best-effort: a writer failure
    is logged, never raised into the stream.
    """
    terminal_status: str | None = None

    def _record_terminal() -> None:
        status = terminal_status or "cancelled"
        try:
            records.finalize_turn(turn_id=turn_id, owner=owner, status=status)
        except TurnRecordError as exc:
            logger.warning(
                "Turn finalization skipped for %s: %s", turn_id, exc,
            )

    async for event in events:
        kind = getattr(event, "type", None) or getattr(event, "kind", None)
        kind = str(kind or "").replace("_", "").replace(".", "")
        if kind in {"turncompleted", "turncompletedwithapproval"}:
            terminal_status = "completed"
        elif kind in {"turnfailed", "turnaborted"}:
            terminal_status = "failed"
        elif kind in {"turncancelled", "cancelled"}:
            terminal_status = "cancelled"
        elif kind in {"turnapprovalrequired", "approvalrequired"}:
            terminal_status = "awaiting_approval"
        yield event
    _record_terminal()

router = APIRouter(prefix="/api/v1", tags=["gateway"])


class TurnCreateBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    mode: str = Field(default="chat", pattern="^(chat|agent)$")
    message: str = Field(min_length=1, max_length=200_000)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class ApprovalDecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|approve_task|deny)$")


class CancelBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


def _records_for(request: Request) -> Any:
    """The durable record store when the operator enabled it."""
    return getattr(request.app.state, "gateway_turn_records", None)


def _session_manager(request: Request) -> Any:
    return getattr(request.app.state, "session_manager", None)


def _require_owner(request: Request) -> str:
    principal = principal_for_chat_request(request)
    if principal.owner_id is None:
        raise HTTPException(
            status_code=401, detail="Gateway API requires an authenticated owner",
        )
    return principal.owner_id


def _admit(request: Request, session_id: str) -> tuple[Any, Any]:
    """Admit the caller against exactly one owner-scoped session."""
    owner = _require_owner(request)
    manager = _session_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager unavailable")
    getter = getattr(manager, "get_session", None)
    if callable(getter):
        try:
            session = getter(session_id)
        except KeyError:
            session = None
        if session is None and hasattr(manager, "sessions"):
            # Fixtures and in-memory managers keep sessions in the cache
            # only; the real manager hydrates from the database.
            session = manager.sessions.get(session_id)
        if session is None or getattr(session, "owner", None) != owner:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session = manager.sessions.get(session_id)
        if session is None or getattr(session, "owner", None) != owner:
            raise HTTPException(status_code=404, detail="Session not found")
    admission = GatewayAdmission().admit(
        principal_for_chat_request(request), session_id,
        lambda _: SessionOwnership(owner_id=owner),
    )
    return admission, session


def _resolve_run(
    request: Request, turn_id: str, owner: str,
) -> tuple[Any, Any | None]:
    """Resolve a turn id to its typed run through the owner's sessions."""
    from src import agent_runs

    manager = _session_manager(request)
    if manager is None:
        return None, None
    for session in manager.sessions.values():
        if getattr(session, "owner", None) != owner:
            continue
        run = agent_runs.get_matching_run(session.id, turn_id, mode="typed")
        if run is not None:
            return run, session
    return None, None


@router.post("/turns", status_code=201)
async def create_turn(body: TurnCreateBody, request: Request) -> dict:
    """Create one admitted turn with an idempotency key and start it detached.

    Execution runs through the same Gateway facade as the Web adapter;
    this router never calls the Agent Loop directly.
    """
    import uuid

    admission, session = _admit(request, body.session_id)
    if getattr(session, "incognito", False):
        raise HTTPException(
            status_code=403, detail="Incognito sessions are not gateway-turn eligible",
        )
    gateway: GatewayTurns = request.app.state.gateway_turns
    records = _records_for(request)
    turn_id = uuid.uuid4().hex
    if records is not None:
        try:
            records.open_turn(
                owner=admission.principal.owner_id,
                session_id=body.session_id,
                incarnation_id=(
                    getattr(session, "incarnation_id", None) or ""
                ),
                turn_id=turn_id,
                idempotency_key=body.idempotency_key,
            )
        except TurnRecordError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        from routes.chat_helpers import LegacyChatContextPorts

        ports = getattr(request.app.state, "chat_context_ports", None)
        if ports is None:
            ports = LegacyChatContextPorts()
        if not callable(getattr(ports, "extract_preset", None)):
            ports = LegacyChatContextPorts()
        delivered = gateway.open_composed_text_events(
            admission, session,
            context_owner=admission.principal.owner_id,
            request=TextTurnRequest(body.message),
            ports=ports,
            chat_handler=request.app.state.chat_handler,
            chat_processor=request.app.state.chat_processor,
            settings=TrustedTextTurnSettings(interaction_mode=body.mode),
            client_rendering=False,
            records=records,
            turn_id=turn_id,
            persistence=getattr(request.app.state, "text_persistence", None),
        )
    except AdmissionDenied as exc:
        raise HTTPException(
            status_code=int(exc.status_code or 403), detail=str(exc),
        ) from exc

    persistence = getattr(request.app.state, "text_persistence", None)
    if persistence is not None:
        from src.gateway_text_persistence import persist_completed_text_events

        delivered = replace(
            delivered,
            events=persist_completed_text_events(
                delivered.events, delivered.outcome, admission=admission,
                session=session, context_owner=admission.principal.owner_id,
                persistence=persistence,
            ),
        )

    events = delivered.events
    if records is not None:
        events = _finalize_on_terminal(
            events, turn_id, admission.principal.owner_id, records,
        )

    stream = request.app.state.turn_service.start_detached_typed(
        body.session_id, events, delivered.outcome,
        run_id=turn_id,
    )
    return {
        "turn_id": stream.run_id,
        "session_id": body.session_id,
        "status": "running",
    }


@router.get("/turns/{turn_id}")
async def turn_snapshot(turn_id: str, request: Request) -> dict:
    """Owner-scoped turn state snapshot (never executable authority)."""
    owner = _require_owner(request)
    records = _records_for(request)
    if records is not None:
        turn = records.read_turn(turn_id=turn_id, owner=owner)
        if turn is not None:
            return {
                "turn_id": turn["id"],
                "session_id": turn["session_id"],
                "status": turn["status"],
                "terminal": turn["terminal_at"],
            }
    run, _session = _resolve_run(request, turn_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail="Turn not found")
    return {"turn_id": run.run_id, "status": run.status, "terminal": None}


@router.get("/turns/{turn_id}/events")
async def turn_events(turn_id: str, request: Request, after_seq: int = 0) -> StreamingResponse:
    """SSE replay of ordered, sanitized typed events (explicit gaps kept)."""
    owner = _require_owner(request)
    if type(after_seq) is not int or after_seq < 0 or after_seq > 10_000_000:
        raise HTTPException(status_code=422, detail="Invalid after_seq cursor")

    records = _records_for(request)
    if records is not None and (
        records.read_turn(turn_id=turn_id, owner=owner) is not None
    ):
        return StreamingResponse(
            _replay_records(records, turn_id, owner, after_seq),
            media_type="text/event-stream",
        )
    run, _session = _resolve_run(request, turn_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail="Turn not found")
    return StreamingResponse(
        _replay_run_buffer(run, after_seq), media_type="text/event-stream",
    )


@router.post("/turns/{turn_id}/cancel")
async def cancel_turn(turn_id: str, body: CancelBody, request: Request) -> dict:
    """Cancel exactly one owner-scoped turn by its exact id."""
    owner = _require_owner(request)
    records = _records_for(request)
    cancelled = False
    if records is not None:
        turn = records.read_turn(turn_id=turn_id, owner=owner)
        if turn is not None:
            if turn["status"] == "running":
                records.finalize_turn(
                    turn_id=turn_id, owner=owner, status="cancelled",
                )
            cancelled = True
    run, _session = _resolve_run(request, turn_id, owner)
    if run is not None:
        from src import agent_runs

        agent_runs.request_finish(body.session_id, turn_id)
        cancelled = True
    if not cancelled:
        raise HTTPException(status_code=404, detail="Turn not found")
    return {"turn_id": turn_id, "status": "cancelled"}


@router.post("/approvals/{approval_id}/decision")
async def approval_decision(
    approval_id: str, body: ApprovalDecisionBody, request: Request,
) -> dict:
    """One-use owner-checked approval decision against the shared store."""
    session_id = request.query_params.get("session_id")
    if not session_id:
        raise HTTPException(status_code=422, detail="session_id query parameter required")
    admission, session = _admit(request, session_id)
    store = getattr(request.app.state, "tool_approval_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Approval store unavailable")
    try:
        decision = request.app.state.gateway_turns.decide_tool_approval(
            admission, approval_id, body.decision,
            plan_mode=False, internal_tool_request=False, store=store,
            records=_records_for(request), session=session,
        )
    except AdmissionDenied as exc:
        raise HTTPException(
            status_code=int(exc.status_code or 403), detail=str(exc),
        ) from exc
    return {
        "approval_id": str(approval_id),
        "decision": decision.decision,
        "status": "consumed" if decision.decision != "deny" else "denied",
    }


@router.get("/sessions")
async def list_sessions(request: Request) -> dict:
    """Owner-scoped session listing (metadata only)."""
    owner = _require_owner(request)
    manager = _session_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager unavailable")
    items = []
    for session in manager.sessions.values():
        if getattr(session, "owner", None) != owner:
            continue
        items.append({
            "id": session.id,
            "name": getattr(session, "name", None),
            "model": getattr(session, "model", None),
            "mode": getattr(session, "mode", "chat"),
        })
    return {"sessions": items}


@router.get("/models")
async def discovery_models(request: Request) -> dict:
    """Capability discovery: model ids and names, never credentials."""
    _require_owner(request)
    registry = getattr(request.app.state, "model_discovery", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Model discovery unavailable")
    items = registry() if callable(registry) else registry
    return {"models": items or []}


@router.get("/agents")
async def discovery_agents(request: Request) -> dict:
    """Advertised agent surface; discovery never grants execution rights."""
    _require_owner(request)
    return {"agents": [{"id": "chat", "modes": ["chat", "agent"]}]}


@router.get("/tools")
async def discovery_tools(request: Request) -> dict:
    """Advertised tool names; discovery never grants execution rights."""
    _require_owner(request)
    tools = getattr(request.app.state, "tool_discovery", None) or []
    return {"tools": tools}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

async def _replay_records(records: Any, turn_id: str, owner: str, after_seq: int):
    try:
        for item in records.iter_events(
            turn_id=turn_id, owner=owner, after_sequence=after_seq,
        ):
            yield f'data: {json.dumps(item, separators=(",", ":"))}\n\n'
        yield "data: [DONE]\n\n"
    except TurnRecordError:
        yield 'event: error\ndata: {"status":404}\n\n'


async def _replay_run_buffer(run: Any, after_seq: int):
    for event in getattr(run, "buffer", None) or ():
        if getattr(event, "sequence", 0) <= after_seq:
            continue
        yield (
            'data: ' + json.dumps({
                "sequence": event.sequence,
                "kind": event.type,
                "payload": getattr(event, "payload", {}),
            }, separators=(",", ":")) + '\n\n'
        )
    yield "data: [DONE]\n\n"