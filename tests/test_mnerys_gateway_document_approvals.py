"""Owner/session/version/digest-bound active document approvals in the M2.1 Gateway.

All persistent writes and model/tool fakes use a disposable SQLite database.
The application-configured DB and production editor are never opened.
"""

from dataclasses import replace
import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database
from src.agent_runtime.authority import RequestAuthority
from src.gateway_admission import AdmissionDenied
from src.gateway_document_context import (
    load_admitted_active_document, require_sealed_document_snapshot,
)
from src.gateway_turn_composer import ToolApprovalTurnRequest, TextTurnRequest
from src.gateway_turns import GatewayTurns
from src.tool_approvals import ToolApprovalStore, document_content_digest
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_text_sqlite_integration import (
    PG_ENV, isolated_postgresql_engine,
)


@pytest.fixture(params=("sqlite", "postgres"), ids=("SQLite", "PostgreSQL17"))
def document_fixture(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite:///" + str(tmp_path / "active-documents.sqlite"),
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
    else:
        dsn = os.environ.get(PG_ENV)
        if not dsn:
            pytest.skip("Disposable PostgreSQL 17 fixture not supplied")
        engine = isolated_postgresql_engine(dsn)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)

    admission, session, ports, options = configured(monkeypatch)
    doc_id = "editor-document-1"
    with factory.begin() as db:
        db.add(database.Session(
            id=session.id, name="Test", owner="alice",
            endpoint_url=session.endpoint_url, model=session.model,
        ))
        db.add(database.Document(
            id=doc_id, owner="alice", session_id=session.id,
            current_content="Initial paragraph.",
            version_count=3, title="Test document", language="markdown",
            is_active=True, archived=False,
        ))
    options["settings"] = replace(
        options["settings"],
        offered_tools=frozenset({"update_document"}),
        active_document_id=doc_id,
    )
    store = ToolApprovalStore()
    options["approval_store"] = store
    content = "Replacement paragraph."
    origin = RequestAuthority.empty(owner="alice", session_id=session.id)
    pending = store.create(
        owner="alice", session_id=session.id, origin_run_id="fixture-run",
        tool_name="update_document", content=content,
        workspace=None, document_id=doc_id,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("update_document", content),
        selected_tools={"update_document"},
        continuation_query="Replace the active document paragraph.",
        request_text="Replace the active document paragraph.",
        request_authority=origin,
    )
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "approve_task")

    try:
        yield admission, session, ports, options, factory, pending, store, doc_id
    finally:
        database.Base.metadata.drop_all(engine)
        engine.dispose()


async def collect(admission, session, options):
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **options,
    )
    events = [event async for event in delivery.events]
    return delivery, events


@pytest.mark.asyncio
async def test_document_approval_reuses_database_sealed_identity(
    monkeypatch, document_fixture,
):
    import src.turn_service as runtime

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    assert pending.document_id == doc_id
    assert pending.document_version == 3
    assert pending.document_digest == document_content_digest("Initial paragraph.")
    assert pending.owned_operation is not None
    assert pending.backend_operation is not None

    run = []
    async def synthetic_loop(url, model, messages, **kwargs):
        doc = kwargs["active_document"]
        assert doc.id == doc_id
        assert doc.current_content == "Initial paragraph."
        assert doc.owner == "alice"
        assert doc.session_id == session.id
        assert kwargs["request_authority"].bound_to(
            owner="alice", session_id=session.id,
        )
        assert kwargs["turn_contract"].offered == frozenset({"update_document"})
        exact = kwargs["exact_approval"]
        assert exact.pending is pending
        assert exact.claim(
            owner="alice", session_id=session.id, workspace=None,
            tool_name="update_document", content=pending.content,
        )
        run.append(doc)
        yield 'data: {"delta":"Document updated."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", synthetic_loop)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert events[0].type == "approval.decided"
    assert len(run) == 1
    assert store.peek(pending.approval_id) is None
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    with factory() as db:
        assert db.get(database.Document, doc_id).current_content == "Initial paragraph."


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "owner", "session", "version", "content", "inactive", "archived", "deleted",
])
async def test_changed_or_inaccessible_document_keeps_approval_unconsumed(
    document_fixture, mutation,
):
    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    with factory.begin() as db:
        row = db.get(database.Document, doc_id)
        if mutation == "owner":
            row.owner = "mallory"
        elif mutation == "session":
            row.session_id = None
        elif mutation == "version":
            row.version_count += 1
        elif mutation == "content":
            row.current_content += " Modified"
        elif mutation == "inactive":
            row.is_active = False
        elif mutation == "archived":
            row.archived = True
        elif mutation == "deleted":
            db.delete(row)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations and not session.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [
    None, "other-doc", "", "other-user-doc",
])
async def test_unselected_or_unowned_document_does_not_consume(
    document_fixture, selector,
):
    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    if selector == "other-user-doc":
        with factory.begin() as db:
            db.add(database.Document(
                id=selector, owner="mallory", session_id=session.id,
                title="Private", current_content="Private", is_active=True,
                archived=False, version_count=1,
            ))
    options["settings"] = replace(options["settings"], active_document_id=selector)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations


@pytest.mark.asyncio
async def test_document_changed_during_context_preparation_is_rejected(
    document_fixture, monkeypatch,
):
    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    preprocessor = ports.preprocess_message

    async def update_during_context(*args, **kwargs):
        value = await preprocessor(*args, **kwargs)
        with factory.begin() as db:
            row = db.get(database.Document, doc_id)
            row.current_content = "Concurrent edit."
        return value

    monkeypatch.setattr(ports, "preprocess_message", update_during_context)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending


@pytest.mark.asyncio
async def test_document_denial_without_open_editor_does_not_read_database(
    document_fixture, monkeypatch,
):
    from src import gateway_document_context

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    options["settings"] = replace(options["settings"], active_document_id=None)
    options["request"] = ToolApprovalTurnRequest(pending.approval_id, "deny")

    def no_read(**kwargs):
        pytest.fail("Denial should not open the active document")

    monkeypatch.setattr(gateway_document_context, "load_admitted_active_document", no_read)
    delivery, events = await collect(admission, session, options)
    assert [event.type for event in events] == [
        "approval.decided", "message.final", "turn.completed",
    ]
    assert delivery.outcome.state == "completed"
    assert store.peek(pending.approval_id) is None
    assert not ports.operations


@pytest.mark.asyncio
async def test_plain_text_persistence_cannot_enable_document_approval(
    document_fixture,
):
    from src.gateway_text_persistence import TrustedTextPersistence

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    options["persistence"] = TrustedTextPersistence(object(), lambda *a, **k: None)
    with pytest.raises(AdmissionDenied, match="plain text"):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations


@pytest.mark.asyncio
async def test_real_loop_dispatches_only_original_sealed_document_action(
    monkeypatch, document_fixture,
):
    import src.agent_loop as loop
    import src.tool_execution as execution
    from src.agent_runtime.authority import active_request_authority
    from src.tool_approvals import active_tool_approval_store, tool_approval_store

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *args, **kw: 10)
    effects = []

    async def guarded_document_effect(block, **kwargs):
        assert active_tool_approval_store(tool_approval_store) is store
        assert active_request_authority().bound_to(
            owner="alice", session_id=session.id,
        )
        assert block.tool_type == "update_document"
        assert block.content == pending.content
        assert kwargs["approved_document_id"] == doc_id
        assert kwargs["approved_document_version"] == 3
        assert kwargs["approved_document_digest"] == document_content_digest(
            "Initial paragraph."
        )
        effects.append(block.content)
        return "update_document", {"output": "Document updated.", "exit_code": 0}

    async def provider(*args, **kwargs):
        yield 'data: ' + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "exact-doc-action",
                "name": "update_document",
                "arguments": pending.content,
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(execution, "_execute_tool_block_impl", guarded_document_effect)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert events[0].type == "approval.decided"
    assert any(event.type == "tool.completed" for event in events)
    assert effects == [pending.content]
    assert store.peek(pending.approval_id) is None
    with factory() as db:
        row = db.get(database.Document, doc_id)
        assert row.current_content == "Initial paragraph."
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert effects == [pending.content]


@pytest.mark.asyncio
async def test_account_denial_blocks_document_io_before_context(
    document_fixture, monkeypatch,
):
    import src.gateway_turn_composer as composer

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    options["settings"] = replace(
        options["settings"],
        privileges={"can_use_agent": False},
    )

    def unexpected_read(**kwargs):
        pytest.fail("Account denial must precede active-document database I/O")

    monkeypatch.setattr(composer, "load_admitted_active_document", unexpected_read)
    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations and not session.messages


@pytest.mark.asyncio
async def test_ordinary_document_context_is_verified_but_not_persisted(
    document_fixture, monkeypatch,
):
    import src.turn_service as runtime

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    options["request"] = TextTurnRequest(
        "Explain what the current document contains without editing it."
    )
    seen = []

    async def model(url, selected, messages, **kwargs):
        document = kwargs["active_document"]
        assert document.id == doc_id
        assert document.owner == "alice"
        assert document.session_id == session.id
        assert document.current_content == "Initial paragraph."
        assert kwargs["tool_policy"].blocks("bash")
        seen.append(document.content_digest)
        yield 'data: {"delta":"It has one paragraph."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(runtime, "stream_agent_loop", model)
    delivery, events = await collect(admission, session, options)
    assert delivery.outcome.state == "completed"
    assert len(seen) == 1
    assert events[-1].type == "turn.completed"
    assert session.messages == []
    with factory() as db:
        assert db.query(database.ChatMessage).count() == 0


@pytest.mark.asyncio
async def test_database_session_owner_transfer_revokes_document_context(
    document_fixture,
):
    admission, session, ports, options, factory, pending, store, doc_id = document_fixture
    with factory.begin() as db:
        parent = db.get(database.Session, session.id)
        parent.owner = "mallory"

    with pytest.raises(AdmissionDenied):
        await collect(admission, session, options)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations and not session.messages


@pytest.mark.asyncio
async def test_owned_resource_database_failure_is_sanitized_and_unconsumed(
    document_fixture, monkeypatch,
):
    from sqlalchemy.exc import OperationalError
    from src.agent_runtime.owned_resources import BoundOwnedOperation

    admission, session, ports, options, factory, pending, store, doc_id = document_fixture

    def fail_validation(_self):
        raise OperationalError(
            "read secret database endpoint",
            {"password": "SECRET_TEST_SENTINEL"},
            Exception("private connection failed"),
        )

    monkeypatch.setattr(BoundOwnedOperation, "validate", fail_validation)
    with pytest.raises(AdmissionDenied) as captured:
        await collect(admission, session, options)
    assert str(captured.value) == "Document resource identity changed"
    assert "SECRET_TEST_SENTINEL" not in str(captured.value)
    assert store.peek(pending.approval_id) is pending
    assert not ports.operations
