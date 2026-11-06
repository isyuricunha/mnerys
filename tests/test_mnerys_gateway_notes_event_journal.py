"""Safely replayable metadata-only journal across isolated SQLite/PostgreSQL.

No provider, real pending store, production schema or tool executor is used.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from src.gateway_notes_decision_fence import NotesDecisionFence, notes_fence_metadata
from src.gateway_notes_event_journal import (
    NotesEventJournal, NotesJournalEvent, notes_event_journal_metadata,
)
from tests.test_mnerys_gateway_notes_decision_fence import engine as fence_engine

NOW = 1_800_000_000.0
AGE = 30 * 24 * 60 * 60


@pytest.fixture
def engine(fence_engine):
    notes_event_journal_metadata().create_all(fence_engine)
    try:
        yield fence_engine
    finally:
        notes_event_journal_metadata().drop_all(fence_engine)


def fields(approval_id="A" * 43, *, owner="alice", session_id="session-1",
           receipt="user-01", now=NOW):
    return dict(
        owner=owner, session_id=session_id,
        origin_user_receipt=receipt, approval_id=approval_id,
        action_digest="a" * 64, issued_at=now - 1,
        expires_at=now + 300,
    )


def event_fields(approval_id="A" * 43, *, owner="alice", session_id="session-1",
                 receipt="user-01"):
    return dict(
        owner=owner, session_id=session_id,
        origin_user_receipt=receipt, approval_id=approval_id,
    )


def test_schema_is_explicit_and_minimal(engine):
    journal = NotesEventJournal(engine)
    journal.require_schema()
    table = notes_event_journal_metadata().tables["gateway_notes_event_journal"]
    assert {col.name for col in table.columns} == {
        "event_id", "cursor_token", "owner", "session_id", "origin_user_receipt",
        "approval_id", "kind", "created_at",
    }
    assert not set(("tool_input", "tool_output", "payload", "content", "secret")) & {
        col.name for col in table.columns
    }
    assert "ix_notes_journal_scope_cursor" in {
        x["name"] for x in inspect(engine).get_indexes(table.name)
    }


def test_replay_requires_all_three_owner_scope_fields_and_bounded_cursor(engine):
    journal = NotesEventJournal(engine)
    one = journal.append(**event_fields(), kind="review_registered", now=NOW)
    two = journal.append(**event_fields(), kind="decision_reserved", now=NOW + 1)
    assert len(one) == len(two) == 32
    assert one != two
    output = journal.replay(
        owner="ALICE", session_id="session-1", origin_user_receipt="user-01",
    )
    assert output == (
        NotesJournalEvent(one, "review_registered", NOW),
        NotesJournalEvent(two, "decision_reserved", NOW + 1),
    )
    assert journal.replay(
        owner="alice", session_id="session-1",
        origin_user_receipt="user-01", limit=1,
    ) == output[:1]
    assert journal.replay(
        owner="alice", session_id="session-1",
        origin_user_receipt="user-01", after_cursor=one,
    ) == output[1:]
    for key, value in (
        ("owner", "mallory"),
        ("session_id", "session-2"),
        ("origin_user_receipt", "different-user"),
    ):
        assert not journal.replay(**(dict(
            owner="alice", session_id="session-1",
            origin_user_receipt="user-01",
        ) | {key: value}))
    assert "A" * 43 not in repr(output)
    assert "action_digest" not in repr(output)
    assert "tool" not in repr(output)
    assert all(char.isalnum() or char in "-_" for char in one + two)
    with pytest.raises(ValueError):
        journal.replay(
            owner="mallory", session_id="session-1",
            origin_user_receipt="user-01", after_cursor=one,
        )
    with pytest.raises(ValueError):
        journal.replay(
            owner="alice", session_id="session-2",
            origin_user_receipt="user-01", after_cursor=one,
        )


def test_invalid_events_and_duplicate_milestones_fail_closed(engine):
    journal = NotesEventJournal(engine)
    values = event_fields()
    journal.append(**values, kind="review_registered", now=NOW)
    with pytest.raises(IntegrityError):
        journal.append(**values, kind="review_registered", now=NOW + 1)
    for bad in (
        values | {"owner": "\n"},
        values | {"session_id": "../config"},
        values | {"origin_user_receipt": "spaces not accepted"},
        values | {"approval_id": "forged"},
    ):
        with pytest.raises(ValueError):
            journal.append(**bad, kind="review_registered", now=NOW)
    for kind in (
        "approval.requested", "tool.started", "message.delta",
        "message.saved", "tool.execute", "review_approved",
    ):
        with pytest.raises(ValueError):
            journal.append(**values, kind=kind, now=NOW)
    for options in (
        {"after_cursor": -1}, {"after_cursor": True},
        {"after_cursor": "1"}, {"limit": 0},
        {"limit": 101}, {"limit": True},
    ):
        with pytest.raises(ValueError):
            journal.replay(owner="alice", session_id="session-1",
                           origin_user_receipt="user-01", **options)


def test_prune_only_terminal_fences_after_retention(engine):
    journal = NotesEventJournal(engine)
    fence = NotesDecisionFence(engine)
    for idx, state in enumerate(("completed", "reserved", "pending", "denied"), 1):
        approval_id = str(idx) * 43
        session_id = f"session-{idx}"
        kwargs = fields(approval_id, session_id=session_id)
        assert fence.issue(**kwargs, now=NOW)
        values = event_fields(approval_id, session_id=session_id)
        journal.append(**values, kind="review_registered", now=NOW)
        identity = {key: kwargs[key] for key in (
            "owner", "session_id", "origin_user_receipt",
            "approval_id", "action_digest",
        )}
        if state != "pending":
            decision = "deny" if state == "denied" else "approve_task"
            assert fence.reserve(**identity, decision=decision, now=NOW)
            journal.append(**values,
                           kind="decision_denied" if state == "denied" else "decision_reserved",
                           now=NOW + 1)
            if state == "completed":
                assert fence.mark_committed(
                    **identity, assistant_receipt="assistant-01", now=NOW + 2,
                )
                journal.append(**values, kind="assistant_committed", now=NOW + 2)

    with pytest.raises(ValueError):
        journal.prune_terminal(older_than_seconds=AGE - 1, now=NOW + AGE + 1)
    with pytest.raises(ValueError):
        journal.prune_terminal(older_than_seconds=AGE, now=NOW + AGE + 1,
                               max_rows=1001)
    # Batch size enforces bounded work; incomplete pruning can be resumed.
    counts = [
        journal.prune_terminal(older_than_seconds=AGE, now=NOW + AGE + 3,
                               max_rows=2),
        journal.prune_terminal(older_than_seconds=AGE, now=NOW + AGE + 3,
                               max_rows=2),
        journal.prune_terminal(older_than_seconds=AGE, now=NOW + AGE + 3,
                               max_rows=2),
    ]
    assert counts == [2, 2, 1]
    for idx, expected in enumerate((0, 2, 1, 0), 1):
        assert len(journal.replay(
            owner="alice", session_id=f"session-{idx}",
            origin_user_receipt="user-01",
        )) == expected
    # Pruning never destroys or modifies the original effect fences.
    assert fence.status(owner="alice", session_id="session-2",
                        approval_id="2" * 43, now=NOW).state == "reserved"


def test_competing_workers_cannot_append_duplicate_milestone(engine):
    if engine.dialect.name == "sqlite" and engine.url.database in (None, ":memory:"):
        pytest.skip("Shared single connection cannot model independent writers")
    journal = NotesEventJournal(engine)
    def do_append():
        try:
            return journal.append(**event_fields(), kind="review_registered", now=NOW)
        except IntegrityError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [f.result(timeout=20) for f in (
            pool.submit(do_append), pool.submit(do_append),
        )]
    assert sum(value is not None for value in (a, b)) == 1
    assert len(journal.replay(owner="alice", session_id="session-1",
                              origin_user_receipt="user-01")) == 1


def test_cursor_never_recycles_after_terminal_retention(engine):
    fence = NotesDecisionFence(engine)
    journal = NotesEventJournal(engine)
    first = fields("A" * 43)
    assert fence.issue(**first, now=NOW)
    identity = {
        key: first[key]
        for key in (
            "owner", "session_id", "approval_id",
            "action_digest", "origin_user_receipt",
        )
    }
    assert fence.reserve(**identity, decision="deny", now=NOW)
    old_cursor = journal.append(
        **event_fields("A" * 43), kind="decision_denied", now=NOW,
    )
    assert journal.prune_terminal(
        older_than_seconds=AGE, now=NOW + AGE + 1,
    ) == 1
    assert not journal.replay(
        owner="alice", session_id="session-1",
        origin_user_receipt="user-01",
    )
    # Deleting every journal row must not recycle the last cursor value.
    second = fields("B" * 43, session_id="session-2")
    assert fence.issue(**second, now=NOW)
    next_cursor = journal.append(
        **event_fields("B" * 43, session_id="session-2"),
        kind="review_registered", now=NOW,
    )
    assert len(next_cursor) == len(old_cursor) == 32
    assert next_cursor != old_cursor


def test_journal_does_not_automatically_migrate(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "untouched.db"))
    try:
        NotesEventJournal(engine)
        assert not inspect(engine).has_table("gateway_notes_event_journal")
    finally:
        engine.dispose()


def test_metadata_replay_after_engine_reopen(tmp_path):
    target = tmp_path / "metadata-reopen.db"
    dsn = "sqlite+pysqlite:///" + str(target)
    engine = create_engine(dsn)
    try:
        notes_fence_metadata().create_all(engine)
        notes_event_journal_metadata().create_all(engine)
        NotesEventJournal(engine).append(
            **event_fields(), kind="review_registered", now=NOW,
        )
    finally:
        engine.dispose()
    second = create_engine(dsn)
    try:
        events = NotesEventJournal(second).replay(
            owner="alice", session_id="session-1",
            origin_user_receipt="user-01",
        )
        assert len(events) == 1
        assert events[0].kind == "review_registered"
    finally:
        notes_event_journal_metadata().drop_all(second)
        notes_fence_metadata().drop_all(second)
        second.dispose()
