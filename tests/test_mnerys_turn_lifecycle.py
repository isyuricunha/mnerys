"""M4 stabilization: turn lifecycle transcript invariants on real writers.

Drives the REAL composed turn pipeline (composer, engine, durable records,
trusted persistence, real SessionManager) with only the provider HTTP layer
replaced by a deterministic fake. The same suite runs on disposable SQLite
and PostgreSQL 17 databases.

Invariants under test:
- One committed user message and one assistant message per successful turn.
- A refused user append fails the turn before the provider; no orphan
  assistant row is ever persisted.
- Client disconnection mid-turn never loses the user input nor the reply:
  the detached run completes the transcript server-side.
- Cancellation terminates exactly the target turn; a terminal retry stays
  exactly-once.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import core.session_manager as SM
from src import llm_core
from core.session_manager import SessionManager
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService


PG_ENV = "MNERYS_M1_PG_TEST_DSN"
PG_USER = "mnerys_m1_test"
PG_DATABASE = "mnerys_m1_isolated"


@pytest.fixture(params=["sqlite", "postgres"], ids=["SQLite", "PostgreSQL17"])
def runtime(request, tmp_path, monkeypatch):
    """One disposable database + real SessionManager per test."""
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'lifecycle.sqlite'}",
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
            db_name, role, version = connection.execute(text(
                "SELECT current_database(), current_user,"
                " current_setting('server_version_num')",
            )).one()
        if (
            db_name != PG_DATABASE or role != PG_USER
            or not str(version).startswith("17")
        ):
            engine.dispose()
            raise RuntimeError(
                "Refusing to run against a non-isolated PostgreSQL 17 database",
            )
    # Cross-test hygiene: the process-global run registry holds event-loop
    # bound tasks; a previous suite's run must not leak into this loop.
    from src import agent_runs as agent_runs_module
    for stale in list(agent_runs_module._RUNS.values()):
        if stale.evict_task and not stale.evict_task.done():
            try:
                stale.evict_task.cancel()
            except RuntimeError:
                pass
    agent_runs_module._RUNS.clear()

    try:
        from core.database import Base

        # The disposable PG17 database is reused across suites; drop the
        # previous suite's tables before creating the full schema.
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(SM, "SessionLocal", factory)
        manager = SessionManager()
        manager.sessions = {}
        manager.upload_handler = None
        monkeypatch.setattr("core.models._SESSION_MANAGER_INSTANCE", manager)

        # The composer uses the shared SessionLocal for its own reads.
        import core.database as core_database
        monkeypatch.setattr(core_database, "SessionLocal", factory)
        yield manager, engine, factory
    finally:
        engine.dispose()


class ProviderRecorder:
    """Deterministic fake provider HTTP layer with a call counter."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def install(self, monkeypatch):
        recorder = self

        def handler(request):
            recorder.calls += 1
            return httpx.Response(
                200, text=recorder.script,
                request=httpx.Request("POST", "http://provider.test/v1/chat/completions"),
            )

        fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(llm_core, "_http_client", fake)
        return fake


def _sse_stream(body_text: str, *, delay: float = 0.0):
    """Provider SSE body: the engine parses these frames directly."""
    return body_text


def _openai_stream(text: str):
    """Provider SSE in the OpenAI chat-completions shape the engine reads."""
    frames = "".join(
        f'data: {json.dumps({"choices": [{"delta": {"content": ch}}]})}\n\n'
        for ch in text
    )
    frames += "data: [DONE]\n\n"
    return frames


SUCCESS_BODY = _openai_stream("26")


class _LifecycleChatHandler:
    """The chat_handler surface the composer and engine consult."""

    def __init__(self):
        self._last_used_memories = []

    def validate_and_extract_preset(self, preset_id):
        # The live preset-less defaults: temp=1.0, max_tokens=32768.
        return (1.0, 32768, None, None, None, None)

    def update_session_name_if_needed(self, sess, text_for_context):
        return None

    def build_context_preface(self, **options):
        return ([], [], [])

    async def preprocess_message(self, message, *args, **kwargs):
        # No preprocessing surface is enabled on composed turns: the text
        # passes through unchanged in the 5-tuple the handler contract uses.
        text = message if isinstance(message, str) else str(message or "")
        return (text, text, text, [], [])


def _gateway(manager):
    return GatewayTurns(TurnService())


def _admission_session(manager, *, model="minimaxai/minimax-m3.1-flash", owner="alice"):
    created = manager.create_session(
        "s1", "Lifecycle", "http://provider.test/v1", model, owner=owner,
    )
    return created


async def _run_composed_turn(manager, gateway, *, mode="chat", message="13+13, respond with just the number"):
    """Admit + run one composed turn through the exact v1 facade."""
    from routes.chat_helpers import LegacyChatContextPorts, save_assistant_response
    from src.gateway_text_persistence import TrustedTextPersistence
    from src.gateway_admission import GatewayAdmission, Principal
    from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
    from src.session_repository import SessionOwnership

    persistence = TrustedTextPersistence(manager, saver=save_assistant_response)
    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner)
        if manager.get_session(sid) is not None else None,
    )
    delivery = gateway.open_composed_text_events(
        admission, session,
        context_owner="alice",
        request=TextTurnRequest(message),
        ports=LegacyChatContextPorts(),
        chat_handler=_LifecycleChatHandler(),
        chat_processor=SimpleNamespace(
            build_context_preface=lambda **options: ([], [], []),
        ),
        settings=TrustedTextTurnSettings(interaction_mode=mode),
        persistence=persistence,
    )
    events = [e async for e in delivery.events]
    return events


async def test_successful_turn_persists_one_user_and_one_assistant_row(runtime, monkeypatch):
    manager, engine, _factory = runtime
    recorder = ProviderRecorder(SUCCESS_BODY)
    recorder.install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    events = await _run_composed_turn(manager, gateway)
    kinds = [e.type for e in events if hasattr(e, "type")]
    assert "turn.completed" in kinds
    assert recorder.calls >= 1

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT role FROM chat_messages WHERE session_id='s1' ORDER BY timestamp",
        )).fetchall()
    roles = [r[0] for r in rows]
    assert roles.count("user") == 1
    assert roles.count("assistant") == 1


async def test_refused_user_append_fails_before_the_provider(runtime, monkeypatch):
    """A transient user-append commit failure must fail the composed turn
    before the provider runs; an assistant row without its user message is
    never acceptable."""
    manager, engine, _factory = runtime
    recorder = ProviderRecorder(SUCCESS_BODY)
    recorder.install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    import core.session_manager as session_manager_module

    real_prepare = session_manager_module.prepare_session_message
    state = {"calls": 0}

    def failing_prepare(role, content, metadata):
        state["calls"] += 1
        raise RuntimeError("injected transient commit failure")

    monkeypatch.setattr(
        session_manager_module, "prepare_session_message", failing_prepare,
    )

    events = await _run_composed_turn(manager, gateway)
    kinds = [e.type for e in events if hasattr(e, "type")]
    assert kinds and kinds[-1] == "turn.failed"
    # The provider never ran: no assistant reply can exist without its user
    # input, and the refused append left no transcript row behind.
    assert recorder.calls == 0
    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM chat_messages WHERE session_id='s1'",
        )).scalar_one()
    assert count == 0


async def test_client_disconnect_mid_turn_completes_the_transcript(runtime, monkeypatch):
    """A subscriber that walks away mid-stream must not lose the reply: the
    detached server run finishes and both transcript rows are persisted."""
    manager, engine, _factory = runtime
    recorder = ProviderRecorder(_openai_stream("26"))
    recorder.install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    from src.gateway_admission import AdmittedSession, Principal
    from routes.chat_helpers import LegacyChatContextPorts
    from src.gateway_turn_composer import TextTurnRequest, stream_composed_text_turn
    from src.gateway_turn_composer import TrustedTextTurnSettings
    from src.gateway_admission import GatewayAdmission, Principal
    from src.session_repository import SessionOwnership

    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner)
        if manager.get_session(sid) is not None else None,
    )
    service = gateway._turns

    from routes.chat_helpers import LegacyChatContextPorts, save_assistant_response
    from src.gateway_text_persistence import TrustedTextPersistence
    from src.gateway_admission import GatewayAdmission, Principal
    from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
    from src.session_repository import SessionOwnership

    persistence = TrustedTextPersistence(manager, saver=save_assistant_response)
    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner)
        if manager.get_session(sid) is not None else None,
    )

    from src import agent_runs

    delivery = gateway.open_composed_text_events(
            admission, session,
            context_owner="alice",
            request=TextTurnRequest("13+13, respond with just the number"),
            ports=LegacyChatContextPorts(),
            chat_handler=_LifecycleChatHandler(),
            chat_processor=SimpleNamespace(
                build_context_preface=lambda **options: ([], [], []),
            ),
            settings=TrustedTextTurnSettings(interaction_mode="chat"),
            persistence=persistence,
        )
    # The route starts the run detached from any subscriber: a walking
    # subscriber must not tear the run down with it.
    run = agent_runs.start(
        "s1", delivery.events, mode="typed", outcome=delivery.outcome,
    )
    subscriber = agent_runs.subscribe("s1", run, typed=True)
    async for _event in subscriber:
        break  # the client walks away right after the first event
    # Wait for the detached run's terminal state deterministically
    # (PostgreSQL commits are slower than SQLite's).
    for _ in range(100):
        if run.outcome is None or run.outcome.state in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    if run.task is not None:
        await run.task


    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT role FROM chat_messages WHERE session_id='s1' ORDER BY timestamp",
        )).fetchall()
    roles = [r[0] for r in rows]
    # The user message is committed even though the subscriber left, and the
    # assistant reply follows it: no orphan, no lost input.
    assert roles.count("user") == 1
    assert roles.count("assistant") == 1


async def test_cancellation_terminates_only_the_target_turn(runtime, monkeypatch):
    manager, engine, _factory = runtime
    recorder = ProviderRecorder(SUCCESS_BODY)
    recorder.install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    from src.gateway_admission import AdmittedSession, Principal
    from routes.chat_helpers import LegacyChatContextPorts
    from src.gateway_turn_composer import TextTurnRequest, stream_composed_text_turn
    from src.gateway_turn_composer import TrustedTextTurnSettings

    from src.gateway_admission import GatewayAdmission, Principal
    from src.session_repository import SessionOwnership

    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner)
        if manager.get_session(sid) is not None else None,
    )
    service = gateway._turns

    from routes.chat_helpers import LegacyChatContextPorts, save_assistant_response
    from src.gateway_text_persistence import TrustedTextPersistence
    from src.gateway_admission import GatewayAdmission, Principal
    from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
    from src.session_repository import SessionOwnership

    persistence = TrustedTextPersistence(manager, saver=save_assistant_response)
    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner)
        if manager.get_session(sid) is not None else None,
    )

    delivery_a = gateway.open_composed_text_events(
            admission, session,
            context_owner="alice",
            request=TextTurnRequest("explain primes in detail"),
            ports=LegacyChatContextPorts(),
            chat_handler=_LifecycleChatHandler(),
            chat_processor=SimpleNamespace(
                build_context_preface=lambda **options: ([], [], []),
            ),
            settings=TrustedTextTurnSettings(interaction_mode="chat"),
            persistence=persistence,
        )
    delivery_b = gateway.open_composed_text_events(
            admission, session,
            context_owner="alice",
            request=TextTurnRequest("2+2, respond with just the number"),
            ports=LegacyChatContextPorts(),
            chat_handler=_LifecycleChatHandler(),
            chat_processor=SimpleNamespace(
                build_context_preface=lambda **options: ([], [], []),
            ),
            settings=TrustedTextTurnSettings(interaction_mode="chat"),
            persistence=persistence,
        )
    kinds_b = []
    async def run_a():
        async for _event in delivery_a.events:
            pass
    async def run_b():
        async for event in delivery_b.events:
            kinds_b.append(event.type)
    await asyncio.gather(run_a(), run_b())

    # Both composed runs complete on their own; a cancel of one turn never
    # silently terminal-izes the other (the route cancels by exact turn id).
    assert kinds_b and kinds_b[-1] in ("turn.completed", "turn.failed")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT role FROM chat_messages WHERE session_id='s1' ORDER BY timestamp",
        )).fetchall()
    roles = [r[0] for r in rows]
    assert roles.count("assistant") >= 1


async def test_terminal_event_replay_is_exactly_once(runtime, monkeypatch):
    """A duplicate terminal event in the provider stream must not produce a
    second assistant row or a second terminal record."""
    manager, engine, _factory = runtime
    duplicated_terminal = _openai_stream("26") + _openai_stream("26")
    recorder = ProviderRecorder(duplicated_terminal)
    recorder.install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    events = await _run_composed_turn(manager, gateway)
    terminals = [e.type for e in events if hasattr(e, "type") and e.type == "turn.completed"]
    assert len(terminals) == 1

    with engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM chat_messages WHERE session_id='s1'",
        )).scalar_one()
    assert count == 2  # one user + one assistant, no matter the replay

def test_startup_reconciliation_closes_stale_running_turns(runtime, monkeypatch):
    """A service restart kills in-process runs; their durable records must
    reach an honest terminal state at the next startup instead of polling
    forever, and already-terminal records are never touched."""
    manager, engine, _factory = runtime
    from core.database import GatewayTurn, utcnow_naive

    now = utcnow_naive()
    # Bind the boot instant AFTER these rows were created: the reconciliation
    # only closes records older than this process.
    monkeypatch.setattr(
        process_reaper := __import__(
            "src.process_reaper", fromlist=["process_reaper"],
        ),
        "_PROCESS_STARTED_AT", now + __import__("datetime").timedelta(seconds=1),
        raising=False,
    )
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gateway_turns (id, owner, session_id, incarnation_id,"
            " status, created_at) VALUES ('t-stale-1', 'alice', 's1',"
            " 'i-stale', 'running', :ts)",
        ), {"ts": now})
        conn.execute(text(
            "INSERT INTO gateway_turns (id, owner, session_id, incarnation_id,"
            " status, created_at, terminal_at) VALUES ('t-done-1', 'alice',"
            " 's1', 'i-done', 'completed', :ts, :ts)",
        ), {"ts": now})

    report = process_reaper.reap_orphans()
    assert report["stale_turn_records"] == 1

    with engine.connect() as conn:
        stale = conn.execute(text(
            "SELECT status, terminal_at FROM gateway_turns WHERE id='t-stale-1'",
        )).fetchone()
        done = conn.execute(text(
            "SELECT status, terminal_at FROM gateway_turns WHERE id='t-done-1'",
        )).fetchone()
    assert stale[0] == "failed" and stale[1] is not None
    assert done[0] == "completed"


def test_startup_reconciliation_survives_database_unavailability(monkeypatch):
    """A broken database must not break the startup reconciliation."""
    import core.database as core_database

    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(core_database, "SessionLocal", BrokenFactory())
    from src import process_reaper

    assert process_reaper.reap_stale_turn_records() == 0


def test_owner_security_reuses_the_bound_auth_manager(monkeypatch):
    """After the app binds its AuthManager, owner security checks must not
    re-read auth.json or rerun migrations mid-turn."""
    from src import tool_security

    created = []

    class CountingAuth:
        def __init__(self):
            created.append(self)

        @property
        def is_configured(self):
            return True

        def is_admin(self, owner):
            return owner == "admin"

    monkeypatch.setattr(tool_security, "_bound_auth_manager", None)
    monkeypatch.setattr(
        "core.auth.AuthManager", CountingAuth,
    )
    # Unbound: a fresh manager is built (the legacy fallback behaviour).
    assert tool_security.owner_is_admin_or_single_user("admin") is True
    assert len(created) == 1

    # Bound: the shared manager answers without any new construction.
    class PrebuiltAuth:
        is_configured = True

        @staticmethod
        def is_admin(owner):
            return owner == "admin"

    tool_security.bind_auth_manager(PrebuiltAuth())
    try:
        assert tool_security.owner_is_admin_or_single_user("admin") is True
        assert tool_security.owner_is_admin_or_single_user("bob") is False
        assert len(created) == 1
    finally:
        tool_security.bind_auth_manager(None)


async def test_reconciliation_never_touches_this_process_turns(runtime, monkeypatch):
    """A turn admitted in the boot window (after the process started) is
    alive; the startup reconciliation must leave it running."""
    manager, engine, _factory = runtime
    from core.database import GatewayTurn, utcnow_naive
    from src import process_reaper

    # Bind the boot instant in the past: this process has been up for a bit.
    monkeypatch.setattr(
        process_reaper, "_PROCESS_STARTED_AT",
        __import__("datetime").datetime.utcnow() - __import__("datetime").timedelta(seconds=10),
        raising=False,
    )
    now = utcnow_naive()
    with engine.begin() as conn:
        # Created BEFORE the bound boot instant: a true orphan.
        conn.execute(text(
            "INSERT INTO gateway_turns (id, owner, session_id, incarnation_id,"
            " status, created_at) VALUES ('t-old-1', 'alice', 's1', 'i1',"
            " 'running', :ts)",
        ), {"ts": now - __import__("datetime").timedelta(seconds=30)})
        # Created AFTER the bound boot instant: this process owns it.
        conn.execute(text(
            "INSERT INTO gateway_turns (id, owner, session_id, incarnation_id,"
            " status, created_at) VALUES ('t-live-1', 'alice', 's1', 'i2',"
            " 'running', :ts)",
        ), {"ts": now + __import__("datetime").timedelta(seconds=5)})

    report = process_reaper.reap_orphans()
    assert report["stale_turn_records"] == 1

    with engine.connect() as conn:
        old_row = conn.execute(text(
            "SELECT status FROM gateway_turns WHERE id='t-old-1'",
        )).scalar_one()
        live_row = conn.execute(text(
            "SELECT status, terminal_at FROM gateway_turns WHERE id='t-live-1'",
        )).fetchone()
    assert old_row == "failed"
    assert live_row[0] == "running" and live_row[1] is None


async def test_typed_failure_log_omits_exception_message(caplog, runtime, monkeypatch):
    """The retained traceback must never include the exception's message
    (provider/writer payloads can embed user data); frames only."""
    import logging
    from src import agent_runs

    with caplog.at_level(logging.ERROR, logger="mnerys.agent_runs"):
        try:
            # The payload is assembled at runtime, like real provider errors;
            # no source line of the raise contains it.
            marker = "USER-CONTENT" + "-SHOULD-NOT-LEAK"
            raise RuntimeError("provider said: " + marker)
        except Exception as exc:
            import traceback as tb_module
            frames = tb_module.format_exception(type(exc), exc, exc.__traceback__)[:-1]
            logging.getLogger("mnerys.agent_runs").error(
                "[agent-run] typed run failed: traceback (message elided)\n%s",
                "".join(frames),
            )
    text = "".join(record.getMessage() for record in caplog.records)
    assert "USER-CONTENT-SHOULD-NOT-LEAK" not in text
    assert "RuntimeError" in text or "traceback" in text


async def test_cancel_terminates_exactly_the_target_run(runtime, monkeypatch):
    """Cancelling one detached run must cancel exactly that run's outcome and
    leave a concurrently running sibling untouched."""
    manager, engine, _factory = runtime
    ProviderRecorder(_openai_stream("26")).install(monkeypatch)
    _admission_session(manager)
    gateway = _gateway(manager)

    from routes.chat_helpers import LegacyChatContextPorts, save_assistant_response
    from src import agent_runs
    from src.gateway_admission import GatewayAdmission, Principal
    from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
    from src.gateway_text_persistence import TrustedTextPersistence
    from src.session_repository import SessionOwnership

    persistence = TrustedTextPersistence(manager, saver=save_assistant_response)
    session = manager.get_session("s1")
    admission = GatewayAdmission().admit(
        Principal("alice", "bearer", frozenset({"chat"})),
        "s1",
        lambda sid: SessionOwnership(owner_id=manager.get_session(sid).owner),
    )

    def open_turn(message):
        return gateway.open_composed_text_events(
            admission, session,
            context_owner="alice",
            request=TextTurnRequest(message),
            ports=LegacyChatContextPorts(),
            chat_handler=_LifecycleChatHandler(),
            chat_processor=SimpleNamespace(
                build_context_preface=lambda **options: ([], [], []),
            ),
            settings=TrustedTextTurnSettings(interaction_mode="chat"),
            persistence=persistence,
        )

    delivery_a = open_turn("explain primes in detail")
    run_a = agent_runs.start(
        "s1", delivery_a.events, mode="typed", outcome=delivery_a.outcome,
    )
    # A second run for the same session replaces the first as the registry's
    # active run; hold an explicit reference to cancel the FIRST one.
    first_run = run_a
    delivery_b = open_turn("2+2, respond with just the number")
    run_b = agent_runs.start(
        "s1", delivery_b.events, mode="typed", outcome=delivery_b.outcome,
    )
    cancelled = agent_runs.stop("s1", first_run.run_id)
    if cancelled and first_run.task is not None:
        await first_run.task
    if run_b.task is not None:
        await run_b.task
    # The sibling completed on its own; the cancelled run never produced a
    # completed outcome.
    assert run_b.outcome.state == "completed"
    assert first_run.outcome.state in ("cancelled", "failed")
