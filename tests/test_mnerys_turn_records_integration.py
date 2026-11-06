"""M2.3 integration: the composed typed turn persists durable events.

A real composed turn (admission -> composer -> inherited Agent Loop ->
typed adapter) writes its sanitized events and exactly-once terminal into
SqlTurnRecords, and a fresh interpreter can read the finalized turn.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from core.database import (
    GatewayApproval, GatewayTurn, GatewayTurnEvent, Base,
)
from src import turn_service as runtime
from src.gateway_turn_records import SqlTurnRecords, TurnRecordError
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def records_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'records.sqlite'}",
            connect_args={"timeout": 15},
        )
    else:
        dsn = os.environ.get("MNERYS_M1_PG_TEST_DSN")
        if not dsn:
            pytest.skip("Disposable PostgreSQL 17 fixture not supplied")
        url = make_url(dsn)
        if url.host != "127.0.0.1" or url.port is None:
            raise RuntimeError("Refusing a non-loopback PostgreSQL test connection")
        engine = create_engine(
            dsn, connect_args={
                "options": "-c statement_timeout=5000 -c lock_timeout=4000",
            },
        )
        with engine.connect() as connection:
            db_name, role, version = connection.execute(
                text("SELECT current_database(), current_user, current_setting('server_version_num')")
            ).one()
        if db_name != "mnerys_m1_isolated" or role != "mnerys_m1_test" or not str(version).startswith("17"):
            engine.dispose()
            raise RuntimeError("Refusing to run against a non-isolated PostgreSQL 17 database")
    try:
        Base.metadata.create_all(bind=engine, tables=[
            GatewayTurn.__table__, GatewayTurnEvent.__table__,
            GatewayApproval.__table__,
        ])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        yield factory, engine
    finally:
        engine.dispose()


def _records(factory):
    return SqlTurnRecords(factory, GatewayTurn, GatewayTurnEvent, GatewayApproval)


async def _run_composed_turn(records, monkeypatch):
    from src.agent_tool_events import tool_event_frame

    admission, session, _ports, options = configured(monkeypatch)
    session.incarnation_id = "epoch-a"

    async def model(_url, _name, _messages, **kwargs):
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes", "round": 1,
            "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Durable answer."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session,
        records=records,
        ports=_ports,
        chat_handler=options["chat_handler"],
        chat_processor=options["chat_processor"],
        context_owner=admission.principal.owner_id,
        request=options["request"],
        settings=options["settings"],
    )
    events = [event async for event in delivery.events]
    assert delivery.outcome.state == "completed"
    assert delivery.turn_id is not None
    return admission, delivery.turn_id, [event.type for event in events]


@pytest.mark.asyncio
async def test_composed_turn_persists_events_and_finalizes_once(records_engine, monkeypatch):
    factory, _engine = records_engine
    records = _records(factory)
    _admission, turn_id, kinds = await _run_composed_turn(records, monkeypatch)

    turn = records.read_turn(turn_id=turn_id, owner="alice")
    assert turn["status"] == "completed"
    stored = list(records.iter_events(turn_id=turn_id, owner="alice"))
    assert [item["kind"] for item in stored] == kinds
    assert any(item["kind"] == "message.delta" for item in stored)
    assert records.finalize_turn(turn_id=turn_id, owner="alice", status="completed") is False
    assert records.finalize_turn(turn_id=turn_id, owner="alice", status="cancelled") is False
    with pytest.raises(TurnRecordError, match="terminal"):
        records.append_event(
            turn_id=turn_id, owner="alice", sequence=999,
            kind="message.delta", payload={"text": "late"},
        )


@pytest.mark.asyncio
async def test_cancelled_turn_finalizes_as_cancelled(records_engine, monkeypatch):
    factory, _engine = records_engine
    records = _records(factory)
    admission, session, _ports, options = configured(monkeypatch)
    session.incarnation_id = "epoch-a"

    async def model(_url, _name, _messages, **kwargs):
        yield 'data: {"delta":"Partial"}\n\n'
        raise asyncio.CancelledError()

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session,
        records=records,
        ports=_ports,
        chat_handler=options["chat_handler"],
        chat_processor=options["chat_processor"],
        context_owner=admission.principal.owner_id,
        request=options["request"],
        settings=options["settings"],
    )
    with pytest.raises(asyncio.CancelledError):
        [event async for event in delivery.events]
    assert records.read_turn(turn_id=delivery.turn_id, owner="alice")["status"] == "cancelled"


def test_durable_approval_is_sealed_and_consumed_once(records_engine):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.agent_runtime.authority import RequestAuthority
    from src.gateway_admission import (
        AdmissionDenied, GatewayAdmission, Principal, SessionOwnership,
    )
    from src.gateway_turns import GatewayTurns
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    factory, _engine = records_engine
    records = _records(factory)
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1",
        tool_name="manage_notes", content='{"action":"list"}',
        workspace=None, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("manage_notes", '{"action":"list"}'),
        request_authority=RequestAuthority.empty(owner="alice", session_id="session-1"),
    )
    admission = GatewayAdmission().admit(
        Principal("alice", "browser", frozenset()), "session-1",
        lambda _: SessionOwnership(owner_id="alice"),
    )
    session = SimpleNamespace(id="session-1", owner="alice", incarnation_id="epoch-a")
    decision = GatewayTurns(Mock()).decide_tool_approval(
        admission, pending.approval_id, "approve",
        plan_mode=False, internal_tool_request=False, store=store,
        records=records, session=session,
    )
    assert decision.decision == "approve"
    with records.session_factory() as db:
        row = db.get(GatewayApproval, str(pending.approval_id))
        assert row is not None
        assert row.status == "consumed"
        assert row.decision == "approve"
        assert row.owner == "alice"
        assert row.session_id == "session-1"
        assert row.incarnation_id == "epoch-a"
        assert row.turn_id == pending.origin_run_id
        assert row.tool_name == "manage_notes"
        assert row.operation_digest == pending.digest
    # Replay of the same card is refused before any durable write.
    with pytest.raises(AdmissionDenied):
        GatewayTurns(Mock()).decide_tool_approval(
            admission, pending.approval_id, "approve",
            plan_mode=False, internal_tool_request=False, store=store,
            records=records, session=session,
        )
    with records.session_factory() as db:
        row = db.get(GatewayApproval, str(pending.approval_id))
        assert row.status == "consumed"
        assert row.decision == "approve"


def test_finalized_turn_is_readable_by_a_fresh_interpreter(records_engine):
    factory, engine = records_engine
    if engine.dialect.name != "sqlite":
        pytest.skip("Fresh-interpreter subprocess test uses the SQLite file")
    records = _records(factory)
    records.open_turn(
        owner="alice", session_id="session-1", incarnation_id="epoch-a",
        turn_id="restart-turn",
    )
    records.append_event(
        turn_id="restart-turn", owner="alice", sequence=1,
        kind="message.delta", payload={"text": "survives"},
    )
    records.finalize_turn(turn_id="restart-turn", owner="alice", status="completed")
    url = str(engine.url)
    repo = str(Path(__file__).resolve().parents[1])
    env = dict(
        os.environ, DATABASE_URL="sqlite:///:memory:",
        PYTHONPATH=repo + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    script = (
        "from sqlalchemy import create_engine;"
        "from sqlalchemy.orm import sessionmaker;"
        "from core.database import GatewayTurn, GatewayTurnEvent, GatewayApproval, Base;"
        "from src.gateway_turn_records import SqlTurnRecords;"
        f"e = create_engine({url!r});"
        "Base.metadata.create_all(bind=e, tables=[GatewayTurn.__table__, GatewayTurnEvent.__table__, GatewayApproval.__table__]);"
        "r = SqlTurnRecords(sessionmaker(bind=e), GatewayTurn, GatewayTurnEvent, GatewayApproval);"
        "t = r.read_turn(turn_id='restart-turn', owner='alice');"
        "assert t and t['status'] == 'completed', t;"
        "evs = list(r.iter_events(turn_id='restart-turn', owner='alice'));"
        "assert evs[0]['kind'] == 'message.delta' and evs[0]['payload'] == {'text': 'survives'}, evs;"
        "assert r.read_turn(turn_id='restart-turn', owner='bob') is None;"
        "print('TURN_RESTART_READ_OK')"
    )
    job = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=True,
    )
    assert "TURN_RESTART_READ_OK" in job.stdout