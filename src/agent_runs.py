"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

Durability scope: in-memory, survives as long as the server process runs (tab
close / navigation / refresh). It does NOT survive a server restart.
"""
import asyncio
import json
import logging
import traceback
import re
import uuid
from typing import AsyncGenerator, Dict, Optional

from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedRunEvent

logger = logging.getLogger(__name__)


class _Run:
    __slots__ = ("session_id", "buffer", "subscribers", "status", "task", "evict_task", "run_id", "finish_requested", "finish_event", "mode", "outcome", "started", "web_strict_text", "web_native_notes")

    def __init__(
        self, session_id: str, *, mode: str = "sse",
        outcome: Optional[TurnOutcome] = None,
        web_strict_text: bool = False,
        web_native_notes: bool = False,
    ) -> None:
        if (
            type(web_strict_text) is not bool
            or type(web_native_notes) is not bool
            or ((web_strict_text or web_native_notes) and mode != "typed")
            or (web_strict_text and web_native_notes)
        ):
            raise ValueError("Invalid trusted Web typed-run provenance")
        if mode not in {"sse", "typed"}:
            raise ValueError("Unsupported process-local run mode")
        if mode == "typed" and not isinstance(outcome, TurnOutcome):
            raise TypeError("Typed runs require a trusted turn outcome")
        self.session_id = session_id
        self.mode = mode
        self.web_strict_text = web_strict_text
        self.web_native_notes = web_native_notes
        self.outcome = outcome
        self.started = False
        self.buffer: list = []          # original SSE frames or sanitized typed events
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        # Stable across every subscription/replay of this exact detached run.
        # The browser uses it to make local cost accounting replay-idempotent.
        self.run_id: str = uuid.uuid4().hex
        self.finish_requested: bool = False
        self.finish_event = asyncio.Event()


_RUNS: Dict[str, _Run] = {}

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180
_CURSOR_RE = re.compile(r"([0-9a-f]{32}):(0|[1-9][0-9]*)")


def _event_for_client(
    run: _Run, seq: int, event: str | TurnEvent, with_event_ids: bool,
) -> str | TypedRunEvent:
    """Use the same per-run cursor for SSE and typed in-process subscribers."""
    if run.mode == "typed":
        if not isinstance(event, TurnEvent):
            raise TypeError("A typed run contained an invalid event")
        return TypedRunEvent(run.run_id, f"{run.run_id}:{seq}", event)
    if not isinstance(event, str):
        raise TypeError("An SSE run contained an invalid frame")
    if not with_event_ids:
        return event
    return f"id: {run.run_id}:{seq}\n{event}"


def _replay_start(run: _Run, after_cursor: Optional[str]) -> int:
    if after_cursor is None:
        return 0
    if not isinstance(after_cursor, str) or len(after_cursor) > 80:
        raise ValueError("Invalid replay cursor")
    match = _CURSOR_RE.fullmatch(after_cursor)
    if match is None:
        raise ValueError("Invalid replay cursor")
    run_id, sequence = match.groups()
    if run_id != run.run_id:
        raise ValueError("Replay cursor belongs to a different run")
    seq = int(sequence)
    if seq >= len(run.buffer):
        raise ValueError("Replay cursor is ahead of the run")
    return seq + 1


def _publish(run: _Run, ev: str | TurnEvent) -> None:
    """Append one SSE or filtered typed event to the original run buffer."""
    run.buffer.append(ev)
    seq = len(run.buffer) - 1
    for q in list(run.subscribers):
        try:
            q.put_nowait((seq, ev))
        except Exception:
            pass


def _wake_run_subscribers(run: _Run) -> None:
    """Close subscribers even when the drain task never reached its body."""
    for q in list(run.subscribers):
        try:
            q.put_nowait((None, None))
        except Exception:
            pass


def _schedule_evict(session_id: str, expected_run: Optional[_Run] = None) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if expected_run is not None and run is not expected_run:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


def get_run_id(session_id: str) -> Optional[str]:
    """Return the opaque identity of the current detached run, if present."""
    r = _RUNS.get(session_id)
    return r.run_id if r else None


def get_active_run(session_id: str) -> Optional[_Run]:
    """Return the exact active run currently registered for a session."""
    r = _RUNS.get(session_id)
    return r if r and r.status == "running" else None



def get_recoverable_notes_review(session_id: str) -> Optional[_Run]:
    """Locate one paused notes-origin run for an authenticated Web recovery.

    Only the owner-admitted HTTP adapter may call this. There is no token,
    status flag or new authority issued here; the caller must recheck the
    original sealed approval before exposing a card. This is process-local.
    """
    if type(session_id) is not str or not session_id:
        return None
    run = _RUNS.get(session_id)
    return run if (
        run is not None
        and run.web_native_notes is True
        and run.mode == "typed"
        and run.status == "done"
        and isinstance(run.outcome, TurnOutcome)
        and run.outcome.state == "awaiting_approval"
    ) else None


def get_matching_run(
    session_id: str, expected_run_id: str, *, mode: str,
) -> Optional[_Run]:
    """Find one original per-session run, including its terminal replay window."""
    if (
        type(session_id) is not str or not session_id
        or type(expected_run_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", expected_run_id) is None
        or mode not in {"sse", "typed"}
    ):
        return None
    run = _RUNS.get(session_id)
    return run if run and run.run_id == expected_run_id and run.mode == mode else None


def typed_saved_receipt_precedes_cursor(
    session_id: str, expected_run_id: str, after_cursor: str | None,
) -> bool:
    """Prove a receipt was emitted *before* this cursor by the exact run.

    This checks only the original in-memory typed buffer. It does not query
    SQL or create a durable write confirmation.
    """
    run = get_matching_run(session_id, expected_run_id, mode="typed")
    if run is None:
        return False
    start = _replay_start(run, after_cursor)
    return any(
        type(item) is TurnEvent and item.type == "message.saved"
        and isinstance(item.payload, dict)
        and type(item.payload.get("id")) is str
        and bool(item.payload["id"])
        for item in run.buffer[:start]
    )


async def _drain(
    session_id: str, run: _Run, agen: AsyncGenerator[str | TurnEvent, None],
                 prev_task: Optional[asyncio.Task] = None) -> None:
    """Pull every event from the wrapped generator into the run buffer, fanning
    each out to live subscribers. Runs to completion regardless of subscribers."""
    subscribers_woken = False

    def _wake_subscribers() -> None:
        nonlocal subscribers_woken
        if subscribers_woken:
            return
        subscribers_woken = True
        _wake_run_subscribers(run)

    run.started = True
    # If this run replaced an in-flight one (rapid double-send), wait for that
    # one to fully finish first. Its CancelledError handler calls aclose(), which
    # persists its partial response — letting it complete before we start writing
    # keeps the two runs' session saves sequential instead of interleaved.
    try:
        if prev_task is not None and not prev_task.done():
            await asyncio.wait({prev_task})
        terminal_event: str | TurnEvent | None = None
        async for ev in agen:
            # Terminal acknowledgements are held until the original stream
            # unwinds, including its persistence/cleanup work. Both Web SSE
            # and in-process typed streams use this exact one-run barrier.
            if run.mode == "typed":
                if not isinstance(ev, TurnEvent):
                    raise TypeError("Invalid event in a typed run")
                if terminal_event is not None:
                    # The inner turn has already declared a terminal state.
                    continue
                if (
                    type(ev.sequence) is not int
                    or ev.sequence != len(run.buffer) + 1
                ):
                    raise ValueError("Out-of-order typed event")
                if ev.type in {
                    "turn.completed", "turn.failed", "turn.awaiting_approval",
                }:
                    terminal_event = ev
                    continue
            elif not isinstance(ev, str):
                raise TypeError("Invalid frame in an SSE run")
            elif ev.strip() == "data: [DONE]":
                terminal_event = ev
                continue
            _publish(run, ev)
        if run.status == "running":
            run.status = (
                "error" if isinstance(terminal_event, TurnEvent)
                and terminal_event.type == "turn.failed" else "done"
            )
        if terminal_event is not None:
            _publish(run, terminal_event)
    except asyncio.CancelledError:
        run.status = "stopped"
        if run.outcome is not None and run.outcome.state in {"pending", "running"}:
            run.outcome.state = "cancelled"
            run.outcome.failure_code = None
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
        # A rapid third replacement can cancel this task while it is still
        # waiting for its predecessor. Close this run's subscribers promptly,
        # but keep the task alive until the predecessor finishes so the next
        # run still observes the transitive session-save ordering barrier.
        _wake_subscribers()
        if prev_task is not None and not prev_task.done():
            try:
                await asyncio.shield(prev_task)
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        if run.mode == "typed":
            # Do not log trusted-port exception bodies: uploads, provider
            # settings and writer failures may contain private values.
            logger.warning("[agent-run] typed run failed before completion (%s)", type(e).__name__)
            # Targeted diagnostics: keep the traceback frames for diagnosis
            # but drop the final line, whose str(e) can embed provider or
            # writer payloads; the exception type stays in the warning above.
            frames = traceback.format_exception(type(e), e, e.__traceback__)[:-1]
            logger.error(
                "[agent-run] typed run failed: traceback (message elided)\n%s",
                "".join(frames),
            )
        else:
            logger.error("[agent-run] %s failed: %s", session_id, e, exc_info=True)
        run.status = "error"
        if run.mode == "typed":
            # Keep the same typed failure contract, never leak an exception
            # or append Web SSE into an in-process typed buffer.
            sequence = len(run.buffer) + 1
            if run.outcome is not None:
                run.outcome.state = "failed"
                run.outcome.failure_code = "stream_exception"
                run.outcome.last_sequence = sequence
            _publish(run, TurnEvent(
                sequence, "turn.failed",
                {"code": "stream_exception", "status": None},
            ))
        else:
            _publish(
                run,
                "event: error\n"
                f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
            )
            _publish(run, "data: [DONE]\n\n")
    finally:
        # Wake every subscriber with the end sentinel so their SSE closes.
        _wake_subscribers()
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
        _schedule_evict(session_id, run)


def start(
    session_id: str, agen: AsyncGenerator[str | TurnEvent, None], *,
    mode: str = "sse", outcome: Optional[TurnOutcome] = None,
    web_strict_text: bool = False,
    web_native_notes: bool = False,
    run_id: str | None = None,
) -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first.

    ``run_id`` (exactly 32 hex chars) lets a durable Gateway turn and its
    process-local run share one identity; callers without a durable turn
    keep the generated id.
    """
    if run_id is not None and (
        type(run_id) is not str or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
    ):
        raise ValueError("Invalid detached run id")
    # Validate the new run before cancelling an active one.
    run = _Run(
        session_id, mode=mode, outcome=outcome,
        web_strict_text=web_strict_text,
        web_native_notes=web_native_notes,
    )
    if run_id is not None:
        run.run_id = run_id
    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    if prev:
        if prev.task and not prev.task.done():
            # A task cancelled before its first instruction never enters
            # _drain(), so its except/finally blocks cannot update status or
            # wake a response already bound to this exact run. Terminalize it
            # synchronously before cancelling; _drain's cleanup is idempotent
            # when the task had already started.
            if prev.status == "running":
                prev.status = "stopped"
                if prev.outcome is not None and prev.outcome.state in {"pending", "running"}:
                    prev.outcome.state = "cancelled"
                    prev.outcome.failure_code = None
                _wake_run_subscribers(prev)
            prev.task.cancel()
            prev_task = prev.task   # new run awaits this before it starts writing
        if prev.evict_task and not prev.evict_task.done():
            try:
                prev.evict_task.cancel()
            except RuntimeError:
                # A previous run from a closed event loop (test isolation or
                # a reload) cannot accept a cancel callback anymore.
                pass
    _RUNS[session_id] = run
    run.task = asyncio.create_task(_drain(session_id, run, agen, prev_task))
    return run


async def subscribe(
    session_id: str,
    expected_run: Optional[_Run] = None,
    *,
    after_cursor: Optional[str] = None,
    with_event_ids: bool = False,
    typed: bool = False,
) -> AsyncGenerator[str | TypedRunEvent, None]:
    """Replay SSE or filtered typed events from one original per-session run.

    Existing SSE subscribers retain byte-for-byte frames. Typed in-process
    subscribers receive TypedRunEvent with the same run-scoped cursor.
    Both cursors are in-memory only, NOT restart-durable.

    ``expected_run`` binds a lazy StreamingResponse body to the exact run
    whose identity was put in its response headers, even after replacement.
    Cross-session run references and foreign/stale cursors are rejected.
    """
    run = expected_run or _RUNS.get(session_id)
    if run is None:
        if after_cursor is not None:
            raise ValueError("Replay run is no longer available")
        return
    if run.session_id != session_id:
        raise ValueError("Replay run belongs to a different session")
    if typed != (run.mode == "typed") or (typed and with_event_ids):
        raise ValueError("Replay run has a different event representation")
    next_seq = _replay_start(run, after_cursor)
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        while next_seq < len(run.buffer):
            yield _event_for_client(run, next_seq, run.buffer[next_seq], with_event_ids)
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                seq, ev = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. SSE comments are ignored by the UI but reset
                # browser/proxy idle timers, which prevents "empty response"
                # disconnects on llama.cpp first-token latencies of 30s+.
                if run.status == "running":
                    heartbeat_idx += 1
                    yield f": heartbeat {heartbeat_idx}\n\n"
                    continue
                seq, ev = (None, None)
            if seq is None:            # end sentinel
                while next_seq < len(run.buffer):   # flush any tail the sentinel raced
                    yield _event_for_client(run, next_seq, run.buffer[next_seq], with_event_ids)
                    next_seq += 1
                break
            if seq >= next_seq:        # skip events already replayed from the buffer
                yield _event_for_client(run, seq, ev, with_event_ids)
                next_seq = seq + 1
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id, run)


def stop(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Cancel the matching in-flight run (which saves its partial output).

    A stale browser may issue Stop after another tab has replaced the session's
    run. Once the caller knows its opaque run identity, fail closed rather than
    cancelling that newer run.
    """
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    if run and run.task and not run.task.done():
        if not run.started:
            # A task cancelled before its first instruction never executes
            # _drain's except/finally. Terminalize it synchronously.
            run.status = "stopped"
            if run.outcome is not None and run.outcome.state in {"pending", "running"}:
                run.outcome.state = "cancelled"
                run.outcome.failure_code = None
            _wake_run_subscribers(run)
            _schedule_evict(session_id, run)
        run.task.cancel()
        return True
    return False


def request_finish(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Ask the exact active run to finish after its completed editor work."""
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    if run.status != "running" or not run.task or run.task.done():
        return False
    run.finish_requested = True
    run.finish_event.set()
    return True


def should_finish(session_id: str) -> bool:
    run = _RUNS.get(session_id)
    return bool(run and run.status == "running" and run.finish_requested)


def get_finish_event(session_id: str) -> Optional[asyncio.Event]:
    run = _RUNS.get(session_id)
    return run.finish_event if run and run.status == "running" else None
