"""M2.3 durable turn/event/approval record repository acceptance.

Disposable SQLite and the dedicated disposable PostgreSQL 17 harness; no
production database or application schema is touched.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from core.database import (
    GatewayApproval, GatewayTurn, GatewayTurnEvent, Base,
)
from src.gateway_turn_records import SqlTurnRecords, TurnRecordError

PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def records(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'records.sqlite'}",
            connect_args={"timeout": 15},
        )
    else:
        dsn = os.environ.get(PG_ENV)
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
        if db_name != PG_DATABASE or role != PG_USER or not str(version).startswith("17"):
            engine.dispose()
            raise RuntimeError("Refusing to run against a non-isolated PostgreSQL 17 database")
    try:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine, tables=[
            GatewayTurn.__table__, GatewayTurnEvent.__table__,
            GatewayApproval.__table__,
        ])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        yield SqlTurnRecords(factory, GatewayTurn, GatewayTurnEvent, GatewayApproval)
    finally:
        engine.dispose()


TURN = "turn-1"


def _open(records, owner="alice", turn_id=TURN, key=None):
    return records.open_turn(
        owner=owner, session_id="session-1", incarnation_id="epoch-a",
        turn_id=turn_id, idempotency_key=key,
    )


def test_turn_lifecycle_with_ordered_sanitized_events(records):
    _open(records)
    assert records.append_event(
        turn_id=TURN, owner="alice", sequence=1, kind="message.delta",
        payload={"text": "hi"},
    ) == 1
    records.append_event(
        turn_id=TURN, owner="alice", sequence=2, kind="tool.started",
        payload={"tool": "manage_notes", "round": 1},
    )
    assert records.finalize_turn(turn_id=TURN, owner="alice", status="completed") is True
    turn = records.read_turn(turn_id=TURN, owner="alice")
    assert turn["status"] == "completed"
    assert turn["terminal_at"] is not None
    events = list(records.iter_events(turn_id=TURN, owner="alice"))
    assert [item["sequence"] for item in events] == [1, 2]
    assert events[0]["payload"] == {"text": "hi"}


def test_finalize_is_exactly_once_and_blocks_late_events(records):
    _open(records)
    records.append_event(turn_id=TURN, owner="alice", sequence=1, kind="message.delta", payload={"text": "a"})
    assert records.finalize_turn(turn_id=TURN, owner="alice", status="cancelled") is True
    assert records.finalize_turn(turn_id=TURN, owner="alice", status="cancelled") is False
    assert records.finalize_turn(turn_id=TURN, owner="alice", status="completed") is False
    with pytest.raises(TurnRecordError, match="terminal"):
        records.append_event(turn_id=TURN, owner="alice", sequence=2, kind="message.delta", payload={"text": "b"})


def test_duplicate_sequence_is_immutable_and_gaps_are_visible(records):
    _open(records)
    records.append_event(turn_id=TURN, owner="alice", sequence=1, kind="message.delta", payload={"text": "a"})
    records.append_event(turn_id=TURN, owner="alice", sequence=3, kind="message.delta", payload={"text": "c"})
    with pytest.raises(TurnRecordError, match="already recorded"):
        records.append_event(turn_id=TURN, owner="alice", sequence=1, kind="message.delta", payload={"text": "rewritten"})
    events = list(records.iter_events(turn_id=TURN, owner="alice"))
    assert [item["sequence"] for item in events] == [1, 3]
    records.finalize_turn(turn_id=TURN, owner="alice", status="completed")
    partial = list(records.iter_events(turn_id=TURN, owner="alice", after_sequence=1))
    assert [item["sequence"] for item in partial] == [3]


def test_idempotency_key_reuse_requires_the_same_turn(records):
    _open(records, key="retry-1")
    assert _open(records, key="retry-1") == TURN
    with pytest.raises(TurnRecordError, match="Idempotency"):
        records.open_turn(
            owner="alice", session_id="session-1", incarnation_id="epoch-a",
            turn_id="turn-2", idempotency_key="retry-1",
        )


def test_owner_isolation_for_reads_and_writes(records):
    _open(records)
    assert records.read_turn(turn_id=TURN, owner="bob") is None
    with pytest.raises(TurnRecordError, match="another owner"):
        records.append_event(turn_id=TURN, owner="bob", sequence=1, kind="message.delta", payload={"text": "x"})
    with pytest.raises(TurnRecordError, match="another owner"):
        list(records.iter_events(turn_id=TURN, owner="bob"))


def test_approvals_seal_and_decide_exactly_once(records):
    _open(records)
    records.record_approval(
        approval_id="approval-1", owner="alice", session_id="session-1",
        incarnation_id="epoch-a", turn_id=TURN, tool_name="manage_notes",
        operation_digest="d" * 64, resource_identity="note:1",
        effective_authority="authority-1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with pytest.raises(TurnRecordError, match="already exists"):
        records.record_approval(
            approval_id="approval-1", owner="alice", session_id="session-1",
            incarnation_id="epoch-a", turn_id=TURN, tool_name="manage_notes",
            operation_digest="d" * 64, resource_identity=None,
            effective_authority=None, expires_at=None,
        )
    assert records.decide_approval(approval_id="approval-1", owner="alice", decision="approve") is True
    assert records.decide_approval(approval_id="approval-1", owner="alice", decision="deny") is False
    assert records.decide_approval(approval_id="approval-1", owner="bob", decision="deny") is False
    with records.session_factory() as db:
        row = db.get(GatewayApproval, "approval-1")
        assert row.status == "consumed"
        assert row.decision == "approve"


def test_pending_approvals_expire(records):
    _open(records)
    records.record_approval(
        approval_id="old", owner="alice", session_id="session-1",
        incarnation_id="epoch-a", turn_id=TURN, tool_name="bash",
        operation_digest="a" * 64, resource_identity=None,
        effective_authority=None,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    records.record_approval(
        approval_id="fresh", owner="alice", session_id="session-1",
        incarnation_id="epoch-a", turn_id=TURN, tool_name="bash",
        operation_digest="b" * 64, resource_identity=None,
        effective_authority=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert records.expire_pending_approvals(owner="alice") == 1
    with records.session_factory() as db:
        assert db.get(GatewayApproval, "old").status == "expired"
        assert db.get(GatewayApproval, "fresh").status == "pending"


def test_prune_terminal_turns_honors_retention(records):
    for turn_id, seconds_ago in [("old-turn", 3600), ("recent-turn", 5)]:
        records.open_turn(
            owner="alice", session_id="session-1", incarnation_id="epoch-a",
            turn_id=turn_id,
        )
        records.append_event(turn_id=turn_id, owner="alice", sequence=1, kind="message.delta", payload={"text": "x"})
        records.finalize_turn(turn_id=turn_id, owner="alice", status="completed")
    if hasattr(records, "_backdate"):
        pass
    # Backdate the old terminal row so the retention window applies.
    with records.session_factory() as db:
        row = db.get(GatewayTurn, "old-turn")
        row.terminal_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    assert records.prune_terminal_turns(
        before=datetime.now(timezone.utc) - timedelta(minutes=10),
    ) == 1
    assert records.read_turn(turn_id="old-turn", owner="alice") is None
    assert records.read_turn(turn_id="recent-turn", owner="alice") is not None


def test_invalid_ids_and_payloads_fail_before_mutation(records):
    with pytest.raises(TurnRecordError, match="Invalid owner"):
        _open(records, owner="bad owner!")
    _open(records)
    with pytest.raises(TurnRecordError, match="payload"):
        records.append_event(turn_id=TURN, owner="alice", sequence=1, kind="x", payload="not-a-dict")
    with pytest.raises(TurnRecordError, match="payload"):
        records.append_event(turn_id=TURN, owner="alice", sequence=1, kind="x", payload={"secret": "x" * 65536})