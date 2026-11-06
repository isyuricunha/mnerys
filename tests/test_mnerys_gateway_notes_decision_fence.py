"""A non-authorizing durable exact-decision fence across SQLite/PostgreSQL.

All database engines are isolated test fixtures, never the application engine.
The future execution path is not wired to this primitive until the original
sealed approval/receipt and migration contracts have complete acceptance.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.gateway_notes_decision_fence import (
    NotesDecisionFence, DecisionFenceStatus, notes_fence_metadata,
)


def review(
    approval_id="A" * 43, *, owner="alice", session_id="session-1",
    receipt="committed-user-001", digest="f" * 64, now=1_800_000_000.0,
):
    return {
        "owner": owner, "session_id": session_id,
        "approval_id": approval_id, "action_digest": digest,
        "origin_user_receipt": receipt,
        "issued_at": now - 1, "expires_at": now + 300,
    }


@pytest.fixture(params=["sqlite_file", "sqlite_memory", "postgresql"])
def engine(request, tmp_path):
    if request.param == "postgresql":
        dsn = os.getenv("MNERYS_M21_PG_TEST_DSN")
        if not dsn or os.getenv("MNERYS_M21_PG_TEST_ISOLATED") != "1":
            pytest.skip("No explicitly isolated PostgreSQL test backend")
        url = make_url(dsn)
        if not (
            url.get_backend_name() == "postgresql"
            and url.host == "127.0.0.1"
            and url.database == "mnerys_m21_isolated"
            and url.username == "mnerys_m21_test"
        ):
            raise AssertionError("Unsafe or unexpected PostgreSQL test database")
        db = create_engine(dsn, pool_pre_ping=True)
    elif request.param == "sqlite_memory":
        from sqlalchemy.pool import StaticPool
        db = create_engine(
            "sqlite+pysqlite://",
            poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
    else:
        db = create_engine(
            "sqlite+pysqlite:///" + str(tmp_path / "notes-fence.db"),
            connect_args={"timeout": 20},
        )
    try:
        notes_fence_metadata().create_all(db)
        yield db
    finally:
        notes_fence_metadata().drop_all(db)
        db.dispose()


def test_schema_is_metadata_only_and_has_exact_pending_unique_index(engine):
    table = notes_fence_metadata().tables["gateway_notes_decision_fences"]
    columns = {column.name for column in table.columns}
    assert columns == {
        "approval_id", "owner", "session_id", "origin_user_receipt",
        "action_digest", "profile", "state", "issued_at",
        "expires_at", "updated_at", "assistant_receipt",
    }
    # No private note bodies, tool command, secrets, request texts or payloads.
    assert "content" not in columns and "payload" not in columns
    assert not {"api_key", "credential", "tool_input", "request_text"} & columns
    assert "ux_notes_fence_pending_session" in {
        i["name"] for i in inspect(engine).get_indexes(table.name)
    }
    NotesDecisionFence(engine).require_schema()


def test_owner_and_receipt_bound_atomic_single_reservation(engine):
    fence = NotesDecisionFence(engine)
    kwargs = review()
    now = 1_800_000_000.0
    assert fence.issue(**kwargs, now=now)
    assert fence.status(owner="alice", session_id="session-1", approval_id=kwargs["approval_id"], now=now).state == "pending"
    reserved = {
        key: kwargs[key] for key in (
            "owner", "session_id", "approval_id", "action_digest", "origin_user_receipt"
        )
    }
    assert not fence.reserve(**(reserved | {"owner": "mallory"}), decision="approve_task", now=now)
    assert not fence.reserve(**(reserved | {"session_id": "session-2"}), decision="approve", now=now)
    assert not fence.reserve(**(reserved | {"action_digest": "e" * 64}), decision="approve", now=now)
    assert not fence.reserve(**(reserved | {"origin_user_receipt": "other-receipt"}), decision="approve", now=now)
    assert fence.reserve(**reserved, decision="approve_task", now=now)
    assert not fence.reserve(**reserved, decision="approve_task", now=now)
    assert not fence.reserve(**reserved, decision="deny", now=now)
    assert not fence.issue(**kwargs, now=now)
    assert not fence.mark_committed(
        **(reserved | {"owner": "mallory"}), assistant_receipt="assistant-1", now=now,
    )
    assert fence.mark_committed(
        **reserved, assistant_receipt="assistant-1", now=now,
    )
    assert not fence.mark_committed(
        **reserved, assistant_receipt="assistant-2", now=now,
    )
    result = fence.status(
        owner="alice", session_id="session-1",
        approval_id=kwargs["approval_id"], now=now,
    )
    assert result == DecisionFenceStatus(
        state="completed", expires_at=kwargs["expires_at"],
        assistant_receipt="assistant-1",
    )
    assert fence.status(
        owner="mallory", session_id="session-1",
        approval_id=kwargs["approval_id"], now=now,
    ) is None


def test_uncertain_reserved_effect_survives_process_reopen_and_cannot_replay(engine):
    first = NotesDecisionFence(engine)
    fields = review()
    now = 1_800_000_000.0
    identity = {key: fields[key] for key in (
        "owner", "session_id", "approval_id", "action_digest",
        "origin_user_receipt",
    )}
    assert first.issue(**fields, now=now)
    assert first.reserve(**identity, decision="approve", now=now)
    # Simulate a process disappearing between the effect and writer receipt.
    del first
    second = NotesDecisionFence(engine)
    assert second.status(
        owner="alice", session_id="session-1",
        approval_id=fields["approval_id"], now=now + 1,
    ).state == "reserved"
    assert not second.reserve(**identity, decision="approve", now=now + 1)
    assert not second.reserve(**identity, decision="deny", now=now + 1)
    assert second.retire_pending(owner="alice", session_id="session-1", now=now + 1) == 0
    assert not second.issue(**fields, now=now + 1)
    # The only safe automated answer after restart is "indeterminate";
    # this module has no tool dispatcher, original approval or effect replay.


def test_expired_or_retired_review_never_grants_a_late_action(engine):
    fence = NotesDecisionFence(engine)
    first = review(now=1_800_000_000.0)
    assert fence.issue(**first, now=1_800_000_000.0)
    assert fence.status(owner="alice", session_id="session-1", approval_id=first["approval_id"], now=1_800_000_301.0).state == "expired"
    identity = {key: first[key] for key in (
        "owner", "session_id", "approval_id", "action_digest",
        "origin_user_receipt",
    )}
    assert not fence.reserve(**identity, decision="approve", now=1_800_000_301.0)
    second = review(approval_id="B" * 43)
    assert fence.issue(**second, now=1_800_000_001.0)
    assert fence.retire_pending(owner="alice", session_id="session-1", now=1_800_000_002.0) == 1
    assert fence.status(owner="alice", session_id="session-1", approval_id=first["approval_id"], now=1_800_000_002.0).state == "superseded"
    assert fence.status(owner="alice", session_id="session-1", approval_id=second["approval_id"], now=1_800_000_002.0).state == "superseded"
    assert not fence.reserve(**identity, decision="approve", now=1_800_000_002.0)


def test_new_review_supersedes_old_pending_not_reserved(engine):
    fence = NotesDecisionFence(engine)
    now = 1_800_000_000.0
    old = review(approval_id="A" * 43)
    new = review(approval_id="B" * 43)
    assert fence.issue(**old, now=now)
    assert fence.issue(**new, now=now)
    assert fence.status(owner="alice", session_id="session-1", approval_id=old["approval_id"], now=now).state == "superseded"
    assert fence.status(owner="alice", session_id="session-1", approval_id=new["approval_id"], now=now).state == "pending"
    identity = {key: new[key] for key in (
        "owner", "session_id", "approval_id", "action_digest",
        "origin_user_receipt",
    )}
    assert fence.reserve(**identity, decision="approve", now=now)
    third = review(approval_id="C" * 43)
    assert fence.issue(**third, now=now)
    assert fence.status(owner="alice", session_id="session-1", approval_id=new["approval_id"], now=now).state == "reserved"
    assert fence.status(owner="alice", session_id="session-1", approval_id=third["approval_id"], now=now).state == "pending"


def test_denial_and_invalid_input_cannot_resurrect_a_review(engine):
    fence = NotesDecisionFence(engine)
    now = 1_800_000_000.0
    data = review()
    identity = {key: data[key] for key in (
        "owner", "session_id", "approval_id", "action_digest",
        "origin_user_receipt",
    )}
    assert fence.issue(**data, now=now)
    assert fence.reserve(**identity, decision="deny", now=now)
    assert fence.status(owner="alice", session_id="session-1", approval_id=data["approval_id"], now=now).state == "denied"
    assert not fence.reserve(**identity, decision="approve", now=now)
    assert not fence.mark_committed(**identity, assistant_receipt="assistant-1", now=now)
    for name, candidate in [
        ("owner", " "), ("session_id", "../secrets"),
        ("approval_id", "forged"), ("action_digest", "not-a-digest"),
        ("origin_user_receipt", "invalid / receipt"),
    ]:
        with pytest.raises(ValueError):
            fence.reserve(**(identity | {name: candidate}), decision="approve", now=now)
    with pytest.raises(ValueError):
        fence.reserve(**identity, decision="approve_all", now=now)
    assert not fence.issue(**data, now=now)


def test_two_workers_compete_one_wins_exactly_once(engine):
    if engine.dialect.name == "sqlite" and engine.url.database in (None, ":memory:"):
        pytest.skip("Single-connection in-memory SQLite cannot model two workers")
    # Both constructed against the same isolated shared engine, but transact
    # via distinct connections, simulating independent app worker instances.
    first = NotesDecisionFence(engine)
    second = NotesDecisionFence(engine)
    now = 1_800_000_000.0
    pending = review()
    identity = {key: pending[key] for key in (
        "owner", "session_id", "approval_id", "action_digest",
        "origin_user_receipt",
    )}
    assert first.issue(**pending, now=now)
    barrier = threading.Barrier(2)

    def claim(fence):
        barrier.wait(timeout=6)
        return fence.reserve(**identity, decision="approve_task", now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(claim, first)
        b = pool.submit(claim, second)
        assert sorted([a.result(timeout=12), b.result(timeout=12)]) == [False, True]
    assert first.status(owner="alice", session_id="session-1", approval_id=pending["approval_id"], now=now).state == "reserved"


def test_unicode_account_owner_is_preserved_without_cross_owner_grant(engine):
    fence = NotesDecisionFence(engine)
    now = 1_800_000_000.0
    record = review(owner="Júlia", approval_id="Z" * 43)
    assert fence.issue(**record, now=now)
    assert fence.status(
        owner="JÚLIA", session_id="session-1",
        approval_id=record["approval_id"], now=now,
    ).state == "pending"
    assert fence.status(
        owner="other", session_id="session-1",
        approval_id=record["approval_id"], now=now,
    ) is None
    assert fence.reserve(
        owner="júlia", session_id="session-1",
        approval_id=record["approval_id"],
        action_digest=record["action_digest"],
        origin_user_receipt=record["origin_user_receipt"],
        decision="deny", now=now,
    )
    assert fence.status(
        owner="Júlia", session_id="session-1",
        approval_id=record["approval_id"], now=now,
    ).state == "denied"


def test_non_authorizing_read_only_recovery_tracks_latest_confirmed_origin(engine):
    fence = NotesDecisionFence(engine)
    now = 1_800_000_000.0
    first = review(approval_id="A" * 43)
    assert fence.issue(**first, now=now)
    query = dict(
        owner="alice", session_id="session-1",
        origin_user_receipt="committed-user-001", now=now,
    )
    assert fence.read_only_recovery(**query) == "interrupted"
    assert fence.read_only_recovery(**(query | {"owner": "mallory"})) is None
    assert fence.read_only_recovery(**(query | {"session_id": "session-2"})) is None
    assert fence.read_only_recovery(**(query | {"origin_user_receipt": "other-user"})) is None
    assert fence.read_only_recovery(**(query | {"now": now + 301})) == "expired"

    identity = {
        field: first[field]
        for field in (
            "owner", "session_id", "approval_id", "action_digest",
            "origin_user_receipt",
        )
    }
    assert fence.reserve(**identity, decision="approve_task", now=now)
    assert fence.read_only_recovery(**(query | {"now": now + 600})) == "indeterminate"

    # A different, later pending proposal for the same receipt cannot hide
    # that a previous action may already have been executed.
    second = review(approval_id="B" * 43, now=now + 1)
    assert fence.issue(**second, now=now + 1)
    assert fence.read_only_recovery(**query) == "indeterminate"

    # After a verified assistant save, the original session history ends in
    # an assistant message; the route suppresses this hint anyway.
    assert fence.mark_committed(
        **identity, assistant_receipt="assistant-1", now=now + 2,
    )
    assert fence.read_only_recovery(**query) == "interrupted"
    assert fence.retire_pending(owner="alice", session_id="session-1", now=now + 3) == 1
    assert fence.read_only_recovery(**query) is None

    for invalid in (
        query | {"owner": "\n"},
        query | {"origin_user_receipt": "bad receipt with spaces"},
        query | {"session_id": "../not-a-session"},
    ):
        with pytest.raises(ValueError):
            fence.read_only_recovery(**invalid)


def test_read_only_status_survives_a_fresh_sqlite_engine_after_close(tmp_path):
    """Reopen the same disposable DB without the original Python run objects."""
    database = tmp_path / "recovery-after-dispose.db"
    dsn = "sqlite+pysqlite:///" + str(database)
    original = create_engine(dsn)
    try:
        notes_fence_metadata().create_all(original)
        now = 1_800_000_000.0
        assert NotesDecisionFence(original).issue(
            **review(approval_id="R" * 43), now=now,
        )
    finally:
        original.dispose()

    reopened = create_engine(dsn)
    try:
        fence = NotesDecisionFence(reopened)
        fence.require_schema()
        assert fence.read_only_recovery(
            owner="alice", session_id="session-1",
            origin_user_receipt="committed-user-001", now=now,
        ) == "interrupted"
        assert fence.read_only_recovery(
            owner="mallory", session_id="session-1",
            origin_user_receipt="committed-user-001", now=now,
        ) is None
    finally:
        notes_fence_metadata().drop_all(reopened)
        reopened.dispose()


def test_schema_creation_is_not_part_of_fence_initialization(tmp_path):
    # Deliberately empty disposable database: constructor cannot migrate it.
    db = create_engine("sqlite+pysqlite:///" + str(tmp_path / "empty.db"))
    try:
        fence = NotesDecisionFence(db)
        assert fence is not None
        assert not inspect(db).has_table("gateway_notes_decision_fences")
    finally:
        db.dispose()
