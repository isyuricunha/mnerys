"""Real inherited persistence writers on disposable SQLite and PostgreSQL 17.

Only an externally provisioned isolated loopback test role/database is accepted
for PostgreSQL. Never connect to the configured Mnerys primary database.
"""

import os
from types import SimpleNamespace
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database, models
from core.models import ChatMessage, Session
import core.session_manager as manager_module
from routes import chat_helpers
from src import turn_service as turn_module
from src.gateway_turn_composer import (
    TextTurnRequest, ToolApprovalTurnRequest, TrustedTextTurnSettings,
)
from src.agent_tool_events import approval_requested_event, tool_event_frame
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.gateway_text_persistence import TrustedTextPersistence
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from src.gateway_admission import AdmissionDenied, GatewayAdmission, Principal, SessionOwnership
from test_mnerys_gateway_turn_composer import FakePorts


class LegacySqlitePorts(FakePorts):
    def append_user_message(
        self, session, handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        assert incognito is False
        self.operations.append("append_user")
        session.add_message(ChatMessage(
            "user", preprocessed.user_content,
            metadata={"interaction_mode": interaction_mode},
        ))

    def session_history(self, session):
        return session.get_context_messages()


PG_ENV = "MNERYS_M21_PG_TEST_DSN"
PG_ROLE = "mnerys_m21_test"
PG_DATABASE = "mnerys_m21_isolated"


def isolated_postgresql_engine(dsn):
    # Validate client connection details before even opening the connection.
    url = make_url(dsn)
    if (
        url.get_backend_name() != "postgresql"
        or url.host != "127.0.0.1"
        or not url.port
        or url.database != PG_DATABASE
        or url.username != PG_ROLE
    ):
        raise RuntimeError("Refusing non-isolated PostgreSQL test target")
    engine = create_engine(
        dsn,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=8000 -c lock_timeout=5000",
        },
    )
    try:
        with engine.connect() as connection:
            db_name, role, version = connection.execute(
                text(
                    "SELECT current_database(), current_user, "
                    "current_setting('server_version_num')"
                )
            ).one()
        if (
            db_name != PG_DATABASE
            or role != PG_ROLE
            or not str(version).startswith("17")
        ):
            raise RuntimeError("Refusing a non-isolated PostgreSQL 17 server")
    except BaseException:
        engine.dispose()
        raise
    return engine


@pytest.fixture(params=("sqlite", "postgres"), ids=("SQLite", "PostgreSQL17"))
def isolated_legacy_session_database(request, tmp_path, monkeypatch):
    # Dedicated disposable backend. NEVER use core.database.SessionLocal.
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite:///" + str(tmp_path / "direct-turn.sqlite"),
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
    monkeypatch.setattr(manager_module, "SessionLocal", factory)

    session = Session(
        id="session-1", name="Direct fixture",
        endpoint_url="https://example.com/v1",
        model="fixture-model", owner="alice",
    )
    manager = manager_module.SessionManager.__new__(manager_module.SessionManager)
    manager.sessions = {session.id: session}
    manager.upload_handler = None
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", manager)
    monkeypatch.setattr(manager, "get_session", lambda sid: manager.sessions[sid])
    with factory() as db:
        db.add(database.Session(
            id=session.id, name=session.name, endpoint_url=session.endpoint_url,
            model=session.model, owner="alice", message_count=0,
        ))
        db.commit()

    # Legacy helpers call an unscoped "last accessed" updater; redirect it
    # exclusively into this isolated factory rather than the configured DB.
    def touch(session_id):
        with factory.begin() as db:
            row = db.get(database.Session, session_id)
            if row:
                row.last_accessed = database.utcnow_naive()

    monkeypatch.setattr(database, "update_session_last_accessed", touch)
    monkeypatch.setattr(chat_helpers, "_append_sft_trace_record", lambda **_: None)

    admission = GatewayAdmission().admit(
        Principal("alice", "browser"), session.id,
        lambda _: SessionOwnership(owner_id="alice"),
    )
    ports = LegacySqlitePorts()
    kwargs = dict(
        context_owner="alice",
        request=TextTurnRequest("Explain transaction safety"),
        ports=ports,
        chat_handler=SimpleNamespace(upload_handler=None),
        chat_processor=SimpleNamespace(),
        settings=TrustedTextTurnSettings(),
        persistence=TrustedTextPersistence(
            manager, chat_helpers.save_assistant_response,
            interrupted_writer=chat_helpers.append_interrupted_assistant_response,
        ),
    )

    def persisted():
        with factory() as db:
            rows = db.query(database.ChatMessage).filter_by(
                session_id=session.id
            ).order_by(database.ChatMessage.timestamp).all()
            parent = db.get(database.Session, session.id)
            return [
                (row.role, row.content, row.meta_data, row.id)
                for row in rows
            ], parent.message_count if parent else None

    try:
        yield session, manager, engine, admission, ports, kwargs, persisted
    finally:
        database.Base.metadata.drop_all(engine)
        engine.dispose()


async def collect(delivery):
    return [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_disposable_db_real_legacy_completion_and_reload(
    isolated_legacy_session_database, monkeypatch,
):
    session, _manager, _engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"Committed answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )
    events = await collect(delivery)
    rows, count = persisted()
    assert [row[0:2] for row in rows] == [
        ("user", "Explain transaction safety"),
        ("assistant", "Committed answer"),
    ]
    assert count == len(rows) == 2
    assert [msg.metadata["_db_id"] for msg in session.history] == [
        row[3] for row in rows
    ]
    assert delivery.outcome.state == "completed"
    assert events[-1].type == "turn.completed"

    # A new manager hydrates the same committed transcript from the isolated DB.
    restarted = manager_module.SessionManager()
    loaded = restarted.get_session(session.id)
    assert [item.role for item in loaded.history] == ["user", "assistant"]
    assert [item.content for item in loaded.history] == [
        "Explain transaction safety", "Committed answer",
    ]


@pytest.mark.asyncio
async def test_disposable_db_persisted_native_approval_has_one_confirmed_user_and_reply(
    isolated_legacy_session_database, monkeypatch,
):
    """Exercise the original SQL writers across a paused, resumed notes turn."""
    session, _manager, _engine, admission, ports, kwargs, persisted = (
        isolated_legacy_session_database
    )
    kwargs["request"] = TextTurnRequest("List my notes")
    kwargs["settings"] = replace(
        kwargs["settings"],
        offered_tools=frozenset({"manage_notes"}),
        interaction_mode="agent",
        persist_verified_native_tools=True,
    )
    store = ToolApprovalStore()
    kwargs["approval_store"] = store
    pending = []

    async def awaiting_model(_url, _model, _messages, **options):
        action = '{"action":"list"}'
        approval = store.create(
            owner="alice", session_id=session.id,
            origin_run_id="disposable-sql-review",
            tool_name="manage_notes", content=action, workspace=None,
            request_text="List my notes", continuation_query="List my notes",
            external_untrusted_context_seen=True,
            capabilities=capabilities_for_action("manage_notes", action),
            selected_tools={"manage_notes"},
            request_authority=options["request_authority"],
        )
        pending.append(approval)
        yield approval_requested_event(approval)
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": None, "output": "PRIVATE",
        }, result={"ask_user": approval.public_payload()})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", awaiting_model)
    gateway = GatewayTurns(TurnService())
    first = gateway.open_composed_text_events(admission, session, **kwargs)
    first_events = await collect(first)
    assert first.outcome.state == "awaiting_approval"
    assert first_events[-1].type == "turn.awaiting_approval"
    first_rows, first_count = persisted()
    assert [(row[0], row[1]) for row in first_rows] == [
        ("user", "List my notes"),
    ]
    assert first_count == 1
    assert pending and store.peek(pending[0].approval_id) is pending[0]
    assert session.history[0].metadata["_db_id"] == first_rows[0][3]
    assert ports.operations.count("append_user") == 1

    effects = []

    async def accepted_model(_url, _model, _messages, **options):
        exact = options["exact_approval"]
        assert exact.pending is pending[0]
        assert exact.claim(
            owner="alice", session_id=session.id, tool_name="manage_notes",
            content=pending[0].content, workspace=None,
        )
        effects.append("one previously approved tool")
        yield tool_event_frame({
            "type": "tool_start", "tool": "manage_notes", "round": 1,
        })
        yield tool_event_frame({
            "type": "tool_output", "tool": "manage_notes",
            "round": 1, "exit_code": 0, "output": "PRIVATE",
        })
        yield 'data: {"delta":"Confirmed notes response"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", accepted_model)
    kwargs["request"] = ToolApprovalTurnRequest(pending[0].approval_id, "approve")
    resumed = gateway.open_composed_text_events(admission, session, **kwargs)
    resumed_events = await collect(resumed)
    assert resumed.outcome.state == "completed"
    assert resumed_events[-1].type == "turn.completed"
    assert effects == ["one previously approved tool"]
    assert store.peek(pending[0].approval_id) is None
    assert ports.operations.count("append_user") == 1
    rows, count = persisted()
    assert [(row[0], row[1]) for row in rows] == [
        ("user", "List my notes"),
        ("assistant", "Confirmed notes response"),
    ]
    assert count == 2
    assert [item.metadata["_db_id"] for item in session.history] == [
        row[3] for row in rows
    ]
    with pytest.raises(AdmissionDenied):
        replay = gateway.open_composed_text_events(admission, session, **kwargs)
        await collect(replay)
    assert effects == ["one previously approved tool"]


@pytest.mark.asyncio
async def test_disposable_db_real_interrupted_writer_commits_stopped_metadata(
    isolated_legacy_session_database, monkeypatch,
):
    session, _manager, _engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"visible fragment"}\n\n'
        yield 'event: error\ndata: {"status":500}\n\n'

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )
    events = await collect(delivery)
    rows, count = persisted()
    assert [row[0:2] for row in rows] == [
        ("user", "Explain transaction safety"),
        ("assistant", "visible fragment"),
    ]
    import json
    assert json.loads(rows[-1][2])["stopped"] is True
    assert count == 2
    assert delivery.outcome.state == "failed"
    assert events[-1].type == "turn.failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["user", "assistant"])
async def test_disposable_db_insert_refusal_never_claims_success(
    isolated_legacy_session_database, monkeypatch, phase,
):
    session, _manager, engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database
    insertion = 0

    def refuse(_conn, _cursor, statement, _params, _context, _many):
        nonlocal insertion
        if statement.lstrip().upper().startswith("INSERT INTO CHAT_MESSAGES"):
            insertion += 1
            if phase == "user" or insertion >= 2:
                raise RuntimeError("simulated SQLite insert failure")

    event.listen(engine, "before_cursor_execute", refuse)
    try:
        async def model(*_args, **_kwargs):
            yield 'data: {"delta":"Answer"}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(turn_module, "stream_agent_loop", model)
        delivery = GatewayTurns(TurnService()).open_composed_text_events(
            admission, session, **kwargs,
        )
        events = await collect(delivery)
        assert events[-1].type == "turn.failed"
        assert events[-1].payload["code"] == "persistence_error"
        assert not any(evt.type == "turn.completed" for evt in events)
    finally:
        event.remove(engine, "before_cursor_execute", refuse)
    rows, count = persisted()
    assert len(rows) == (0 if phase == "user" else 1)
    assert count == len(rows)
    assert len(session.history) == len(rows)



@pytest.mark.asyncio
async def test_disposable_db_deleted_session_does_not_acknowledge_late_reply(
    isolated_legacy_session_database, monkeypatch,
):
    session, _manager, _engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database
    from core.session_manager import SessionLocal as fixture_session_factory

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"late response"}\n\n'
        with fixture_session_factory() as db:
            db.delete(db.get(database.Session, session.id))
            db.commit()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )
    events = await collect(delivery)
    assert events[-1].payload["code"] == "persistence_error"
    assert all(e.type != "turn.completed" for e in events)
    rows, count = persisted()
    assert rows == []
    assert count is None


@pytest.mark.asyncio
async def test_disposable_db_owner_transfer_refuses_stale_late_write(
    isolated_legacy_session_database, monkeypatch,
):
    session, _manager, _engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database
    from core.session_manager import SessionLocal as fixture_session_factory

    async def model(*_args, **_kwargs):
        yield 'data: {"delta":"never write across owner transfer"}\n\n'
        with fixture_session_factory.begin() as db:
            row = db.get(database.Session, session.id)
            row.owner = "new-owner"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )
    events = await collect(delivery)
    assert events[-1].payload["code"] == "persistence_error"
    assert all(e.type != "turn.completed" for e in events)
    rows, count = persisted()
    assert [row[0] for row in rows] == ["user"]
    assert count == 1


@pytest.mark.asyncio
async def test_disposable_db_refused_partial_append_has_no_false_receipt(
    isolated_legacy_session_database, monkeypatch,
):
    session, _manager, engine, admission, _ports, kwargs, persisted = isolated_legacy_session_database
    attempted = 0

    def refuse_second_insert(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal attempted
        if statement.lstrip().upper().startswith("INSERT INTO CHAT_MESSAGES"):
            attempted += 1
            if attempted == 2:
                raise RuntimeError("disposable SQLite failed insert")

    event.listen(engine, "before_cursor_execute", refuse_second_insert)
    try:
        async def model(*_args, **_kwargs):
            yield 'data: {"delta":"partial answer"}\n\n'
            yield 'event: error\ndata: {"status":502}\n\n'

        monkeypatch.setattr(turn_module, "stream_agent_loop", model)
        delivery = GatewayTurns(TurnService()).open_composed_text_events(
            admission, session, **kwargs,
        )
        events = await collect(delivery)
        assert [e.type for e in events] == ["message.delta", "turn.failed"]
        assert events[-1].payload == {
            "code": "persistence_error", "status": None,
        }
        assert delivery.outcome.failure_code == "persistence_error"
    finally:
        event.remove(engine, "before_cursor_execute", refuse_second_insert)

    rows, count = persisted()
    assert [row[0] for row in rows] == ["user"]
    assert count == len(rows) == len(session.history) == 1


@pytest.mark.parametrize("dsn", [
    "postgresql+psycopg2://mnerys_m21_test@db.example.invalid:5432/mnerys_m21_isolated",
    "postgresql+psycopg2://mnerys_m21_test@127.0.0.1:5432/real_application",
    "postgresql+psycopg2://different_user@127.0.0.1:5432/mnerys_m21_isolated",
    "postgresql+psycopg2://mnerys_m21_test@127.0.0.1/mnerys_m21_isolated",
    "sqlite:////tmp/unrelated.db",
])
def test_rejects_nonisolated_postgresql_dsn_before_connect(monkeypatch, dsn):
    monkeypatch.setattr(
        __import__(__name__),
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail(
            "Invalid PostgreSQL DSN must be rejected before creating an engine"
        ),
    )
    with pytest.raises(RuntimeError, match="Refusing non-isolated"):
        isolated_postgresql_engine(dsn)
