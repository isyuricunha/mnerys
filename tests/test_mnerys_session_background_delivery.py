"""M1 opt-in background-delivery transaction and cache tests.

The imported database fixture creates synthetic tables only in temporary
SQLite or the dedicated disposable PostgreSQL 17 test container.
The inherited BackgroundToolJobs and production session schema are untouched.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import Column, String
from sqlalchemy.exc import IntegrityError

from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal, SessionOwnership
from src.session_cache_reconciliation import apply_committed_message_to_cache
from src.session_message_preparation import PreparedSessionMessage, prepare_session_message
from src.session_revision import SessionRevisionConflict, SessionRevisionUnavailable
from src.session_write_fencing import (
    FencedDeliveryReceipt,
    SessionWriteFence,
    SqlFencedSessionWriter,
)
from tests.test_mnerys_session_write_fencing import (
    Base, SessionFixture, MessageFixture, database, admitted, messages, row,
)


class JobFixture(Base):
    __tablename__ = "m1_fenced_background_jobs"
    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=False)
    owner = Column(String)
    origin_incarnation_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    message_id = Column(String)


@pytest.fixture
def store(database):
    with database() as db:
        db.add_all([
            JobFixture(
                id="job-1", session_id="s1", owner="alice",
                origin_incarnation_id="epoch-a", status="ready",
            ),
            JobFixture(
                id="job-2", session_id="s1", owner="alice",
                origin_incarnation_id="epoch-a", status="ready",
            ),
            JobFixture(
                id="other-job", session_id="s2", owner="bob",
                origin_incarnation_id="epoch-b", status="ready",
            ),
            JobFixture(
                id="legacy-job", session_id="legacy", owner=None,
                origin_incarnation_id="epoch-legacy", status="ready",
            ),
        ])
        db.commit()
    return database


def writer(factory):
    return SqlFencedSessionWriter(factory, SessionFixture, MessageFixture, JobFixture)


def job(factory, job_id="job-1"):
    with factory() as db:
        return db.get(JobFixture, job_id)


def report(content="Report is ready"):
    return prepare_session_message(
        "assistant", content, {"background_job_id": "job-1"},
    )


def test_background_delivery_and_marker_commit_atomically(store):
    adapter = writer(store)
    grant = admitted()
    initial = adapter.read_fence(grant)
    receipt = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    assert receipt == FencedDeliveryReceipt(
        SessionWriteFence("s1", "epoch-a", 1), receipt.message_id, inserted=True,
    )
    assert row(store).message_count == 1
    assert row(store).revision == 1
    assert job(store).status == "delivered"
    assert job(store).message_id == receipt.message_id
    saved = messages(store)
    assert len(saved) == 1
    assert (saved[0].id, saved[0].role, saved[0].content) == (
        receipt.message_id, "assistant", "Report is ready",
    )


def test_identical_retry_returns_receipt_without_rewriting_message(store):
    adapter = writer(store)
    grant = admitted()
    initial = adapter.read_fence(grant)
    first = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    second = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    assert first.inserted is True
    assert second.inserted is False
    assert second.message_id == first.message_id
    assert second.fence == first.fence
    assert row(store).revision == 1
    assert row(store).message_count == 1
    assert len(messages(store)) == 1


def test_retry_after_unrelated_session_write_is_still_idempotent(store):
    adapter = writer(store)
    grant = admitted()
    initial = adapter.read_fence(grant)
    first = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    after, _ = adapter.append_prepared_message(
        grant, first.fence, prepare_session_message("user", "later"),
    )
    retry = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    assert retry.inserted is False
    assert retry.fence == after
    assert row(store).message_count == 2
    assert len(messages(store)) == 2


def test_replay_with_different_content_fails_closed(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    adapter.deliver_background_result(grant, fence, job_id="job-1", message=report())
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, fence, job_id="job-1", message=report("Different report"),
        )
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, fence, job_id="job-1",
            message=prepare_session_message(
                "assistant", "Report is ready",
                {"background_job_id": "changed"},
            ),
        )
    assert row(store).message_count == 1
    assert len(messages(store)) == 1


def test_stale_fence_cannot_deliver_job_or_increment_counter(store):
    adapter, grant = writer(store), admitted()
    old = adapter.read_fence(grant)
    adapter.append_prepared_message(
        grant, old, prepare_session_message("user", "another turn"),
    )
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, old, job_id="job-1", message=report(),
        )
    assert job(store).status == "ready"
    assert row(store).revision == 1
    assert row(store).message_count == 1


def test_old_job_cannot_follow_deleted_recreated_session(store):
    adapter, grant = writer(store), admitted()
    previous = adapter.read_fence(grant)
    with store() as db:
        db.query(SessionFixture).filter_by(id="s1").delete()
        db.commit()
    with store() as db:
        db.add(SessionFixture(
            id="s1", owner="alice", incarnation_id="epoch-new", revision=0,
            name="Replacement", message_count=0,
        ))
        db.commit()
    new_fence = adapter.read_fence(grant)
    for fence in (previous, new_fence):
        with pytest.raises(SessionRevisionConflict):
            adapter.deliver_background_result(
                grant, fence, job_id="job-1", message=report(),
            )
    assert row(store).message_count == 0
    assert row(store).revision == 0
    assert job(store).status == "ready"
    assert messages(store) == []


def test_owner_change_after_admission_blocks_job_delivery(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    with store() as db:
        db.query(SessionFixture).filter_by(id="s1").update(
            {"owner": "bob"}, synchronize_session=False,
        )
        db.commit()
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, fence, job_id="job-1", message=report(),
        )
    assert row(store).revision == 0
    assert job(store).status == "ready"


def test_foreign_job_is_not_claimable_under_session_admission(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, fence, job_id="other-job", message=report(),
        )
    assert row(store).revision == 0
    assert job(store, "other-job").status == "ready"


def test_duplicate_insert_rolls_back_job_marker_and_session_revision(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    duplicate_id = str(uuid5(
        NAMESPACE_URL, "mnerys:background:s1:epoch-a:job-1"
    ))
    with store() as db:
        db.add(MessageFixture(
            id=duplicate_id, session_id="s1", role="assistant",
            content="An older retained row",
            timestamp=datetime.now(timezone.utc),
        ))
        db.commit()
    with pytest.raises(IntegrityError):
        adapter.deliver_background_result(
            grant, fence, job_id="job-1", message=report(),
        )
    assert row(store).revision == 0
    assert row(store).message_count == 0
    assert job(store).status == "ready"
    assert job(store).message_id is None
    assert [m.content for m in messages(store)] == ["An older retained row"]


def test_two_concurrent_deliveries_from_same_job_insert_only_once(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    gate = Barrier(2)

    def deliver():
        gate.wait(timeout=5)
        try:
            return adapter.deliver_background_result(
                grant, fence, job_id="job-1", message=report(),
            )
        except SessionRevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(deliver)
        b = pool.submit(deliver)
        receipts = [a.result(timeout=15), b.result(timeout=15)]
    assert sum(r is not None and r.inserted for r in receipts) == 1
    assert row(store).revision == 1
    assert row(store).message_count == 1
    assert job(store).status == "delivered"
    assert len(messages(store)) == 1


def test_two_jobs_same_revision_require_a_fresh_fence_between_writes(store):
    adapter, grant = writer(store), admitted()
    fence = adapter.read_fence(grant)
    receipt = adapter.deliver_background_result(
        grant, fence, job_id="job-1", message=report(),
    )
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, fence, job_id="job-2",
            message=prepare_session_message("assistant", "Second job"),
        )
    assert job(store, "job-2").status == "ready"
    next_receipt = adapter.deliver_background_result(
        grant, receipt.fence, job_id="job-2",
        message=prepare_session_message("assistant", "Second job"),
    )
    assert next_receipt.inserted
    assert row(store).revision == 2
    assert row(store).message_count == 2
    assert len(messages(store)) == 2


def test_explicit_single_user_mode_preserves_null_owner_background_job(store):
    adapter = writer(store)
    grant = GatewayAdmission().admit(
        Principal(None, "single_user"), "legacy",
        lambda _: SessionOwnership(None),
    )
    receipt = adapter.deliver_background_result(
        grant, adapter.read_fence(grant), job_id="legacy-job",
        message=prepare_session_message("assistant", "Legacy result"),
    )
    assert receipt.inserted
    assert row(store, "legacy").message_count == 1
    assert job(store, "legacy-job").status == "delivered"


def test_invalid_inputs_fail_before_mutation(store):
    adapter = writer(store)
    grant = admitted()
    fence = adapter.read_fence(grant)
    for fake in (None, "fake-session", replace(grant, session_id="s2")):
        with pytest.raises(AdmissionDenied):
            adapter.deliver_background_result(
                fake, fence, job_id="job-1", message=report(),
            )
    with pytest.raises(ValueError):
        adapter.deliver_background_result(
            grant, fence, job_id="job/id", message=report(),
        )
    with pytest.raises(SessionRevisionConflict):
        adapter.deliver_background_result(
            grant, replace(fence, session_id="s2"), job_id="job-1", message=report(),
        )
    assert row(store).revision == 0
    assert job(store).status == "ready"


def test_unmigrated_background_job_schema_cannot_open_db():
    def forbidden():
        raise AssertionError("No test DB should be touched")

    adapter = SqlFencedSessionWriter(
        forbidden, SessionFixture, MessageFixture, object,
    )
    with pytest.raises(SessionRevisionUnavailable):
        adapter.deliver_background_result(
            admitted(), SessionWriteFence("s1", "epoch-a", 0),
            job_id="job-1", message=report(),
        )


def test_cache_updates_only_after_confirmed_write_with_matching_generation(store):
    adapter, grant = writer(store), admitted()
    initial = adapter.read_fence(grant)
    cached = SimpleNamespace(
        id="s1", owner="alice", incarnation_id="epoch-a", revision=0,
        message_count=0, history=[], _history=[],
    )
    message = SimpleNamespace(role="assistant", content="Report is ready", metadata=None)
    assert message.metadata is None
    receipt = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    assert apply_committed_message_to_cache(
        grant, cached, previous_fence=initial,
        receipt=receipt, message=message,
    )
    assert message.metadata["_db_id"] == receipt.message_id
    assert cached.history == [message]
    assert cached._history is cached.history
    assert cached.revision == 1
    assert cached.message_count == 1

    replay = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    assert not apply_committed_message_to_cache(
        grant, cached, previous_fence=initial, receipt=replay,
        message=SimpleNamespace(role="assistant", metadata=None),
    )
    assert len(cached.history) == 1


def test_cache_rejects_stale_partial_and_wrong_identity_without_mutation(store):
    adapter, grant = writer(store), admitted()
    initial = adapter.read_fence(grant)
    receipt = adapter.deliver_background_result(
        grant, initial, job_id="job-1", message=report(),
    )
    msg = SimpleNamespace(role="assistant", content="example", metadata=None)
    scenarios = [
        dict(id="s1", owner="alice", incarnation_id="stale", revision=0, message_count=0, history=[]),
        dict(id="s1", owner="bob", incarnation_id="epoch-a", revision=0, message_count=0, history=[]),
        dict(id="s1", owner="alice", incarnation_id="epoch-a", revision=4, message_count=0, history=[]),
        dict(id="s1", owner="alice", incarnation_id="epoch-a", revision=0, message_count=2, history=[]),
        dict(id="s2", owner="alice", incarnation_id="epoch-a", revision=0, message_count=0, history=[]),
    ]
    for props in scenarios:
        cached = SimpleNamespace(**props)
        original = dict(vars(cached))
        assert not apply_committed_message_to_cache(
            grant, cached, previous_fence=initial, receipt=receipt, message=msg,
        )
        assert vars(cached) == original
        assert msg.metadata is None
