"""First transport-neutral service seam around the inherited agent runtime.

This is an *internal* service for already-admitted server requests, not the
future authenticated Gateway API. Stream payloads default to legacy SSE frames; admitted local consumers may opt
into native observations, not the proposed durable Gateway event schema. Admission, session ownership,
tool policy, trusted RequestAuthority construction and persistence are owned by the
existing server routes/runtime. A CLI must not call this directly.

The service reuses the original agent loop, bridge bindings and detached-run
manager; it deliberately does not implement another agent or event store.
The replay buffer remains process-local and the existing session replacement
behavior is unchanged.
"""

from contextlib import aclosing, nullcontext
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterator, Optional

from src import agent_runs
from src.agent_core_events import CoreNativeEvent
from src.gateway_turn_events import TurnEvent, TurnOutcome, TypedRunEvent
from src.agent_tool_events import bind_native_tool_events
from src.tool_approvals import bind_tool_approval_store
from src.agent_loop import stream_agent_loop
from src.tool_execution import bind_execution_bridge
from src.turn_contract import bind_turn_contract


@dataclass(frozen=True)
class TurnStream:
    """An internal run handle; events still contain legacy SSE frames."""

    run_id: str
    events: AsyncIterator[str]


@dataclass(frozen=True)
class TypedTurnStream:
    """Exact process-local run with typed replay and the shared outcome."""

    run_id: str
    events: AsyncIterator[TypedRunEvent]
    outcome: TurnOutcome
    # Server-derived from the same original run buffer, never client input.
    prior_saved_receipt: bool = False


class TurnService:
    """Coordinate an admitted agent turn and its in-process run lifecycle.

    These methods are *not* an authorization boundary. Clients must enter
    through the server's authenticated, owner-scoped Gateway/route adapter.
    The eventual Gateway will own principal admission and durable turn state.
    """

    async def stream_agent(self, bridge, *args, **kwargs) -> AsyncGenerator[str | CoreNativeEvent, None]:
        """Use the inherited loop with the same turn and bridge context.

        In particular, context managers remain active until the stream closes
        or raises; they are not allowed to leak into a later model/tool turn.
        Tool dispatch still enforces the server-created request authority.
        """
        native_tools = kwargs.pop("native_tool_events", False)
        approval_store = kwargs.pop("approval_store", None)
        with (bind_tool_approval_store(approval_store), bind_native_tool_events(native_tools),
              bind_turn_contract(kwargs.get("turn_contract"))):
            # Close the inner generator BEFORE restoring its security context.
            # A caller may close the outer stream immediately after a chunk.
            bridge_binding = (
                bind_execution_bridge(bridge) if bridge is not None else nullcontext()
            )
            with bridge_binding:
                async with aclosing(stream_agent_loop(*args, **kwargs)) as stream:
                    async for chunk in stream:
                        yield chunk

    def start_detached(
        self, session_id: str, stream: AsyncGenerator[str, None], *, run_id=None, on_drained=None,
    ) -> TurnStream:
        """Retain legacy per-session replacement, persistence and SSE replay."""
        run = agent_runs.start(session_id, stream, run_id=run_id)
        if on_drained is not None:
            run.task.add_done_callback(lambda task: on_drained("stopped" if task.cancelled() else run.status))
        return TurnStream(
            run_id=run.run_id,
            events=agent_runs.subscribe(session_id, run),
        )

    def start_detached_typed(
        self, session_id: str, events: AsyncIterator[TurnEvent],
        outcome: TurnOutcome, *,
        web_strict_text: bool = False,
        web_native_notes: bool = False,
        run_id: str | None = None,
    ) -> TypedTurnStream:
        """Use the original detached run manager for trusted typed events.

        ``run_id`` (exactly 32 hex chars) makes a durable turn and its
        process-local run share one identity.
        """
        if (
            not isinstance(outcome, TurnOutcome)
            or outcome.state != "pending"
            or not callable(getattr(events, "aclose", None))
        ):
            raise TypeError("A fresh typed outcome and closable stream are required")
        run = agent_runs.start(
            session_id, events, mode="typed", outcome=outcome,
            web_strict_text=web_strict_text,
            web_native_notes=web_native_notes,
            run_id=run_id,
        )
        return TypedTurnStream(
            run_id=run.run_id, outcome=outcome,
            events=agent_runs.subscribe(session_id, run, typed=True),
        )

    def resume_typed(
        self, session_id: str, expected_run_id: str, *,
        after_cursor: str | None = None,
        web_strict_text: bool = False,
        web_native_notes: bool = False,
        replay_from_start: bool = False,
    ) -> Optional[TypedTurnStream]:
        """Resume exactly one typed run; optionally rebuild a review timeline."""
        if (
            type(expected_run_id) is not str or not expected_run_id
            or len(expected_run_id) > 80
        ):
            return None
        run = agent_runs.get_matching_run(
            session_id, expected_run_id, mode="typed",
        )
        if (
            run is None or not isinstance(run.outcome, TurnOutcome)
            or type(web_strict_text) is not bool
            or type(web_native_notes) is not bool
            or type(replay_from_start) is not bool
            or (web_strict_text and web_native_notes)
            or run.web_strict_text is not web_strict_text
            or run.web_native_notes is not web_native_notes
            or (replay_from_start and not web_native_notes)
        ):
            return None
        # Even when the serializer needs the full original event prefix to
        # reconstruct a pending review, validate the requested cursor against
        # this *exact* run before subscribing to any buffered events.
        prior_saved = agent_runs.typed_saved_receipt_precedes_cursor(
            session_id, expected_run_id, after_cursor,
        )
        return TypedTurnStream(
            run_id=run.run_id, outcome=run.outcome,
            events=agent_runs.subscribe(
                session_id, run,
                after_cursor=None if replay_from_start else after_cursor,
                typed=True,
            ),
            prior_saved_receipt=False if replay_from_start else prior_saved,
        )

    def resume_active(self, session_id: str) -> Optional[TurnStream]:
        """Subscribe to the exact active run; do not switch on replacement."""
        run = agent_runs.get_active_run(session_id)
        if run is None or run.mode != "sse":
            return None
        return TurnStream(
            run_id=run.run_id,
            events=agent_runs.subscribe(session_id, run),
        )

    def stop(self, session_id: str, expected_run_id: Optional[str]) -> bool:
        """Only the exact active run may be cancelled."""
        return agent_runs.stop(session_id, expected_run_id)

    def finish(self, session_id: str, expected_run_id: Optional[str]) -> bool:
        """Only the exact active run may be asked to finish editor work."""
        return agent_runs.request_finish(session_id, expected_run_id)

    def is_active(self, session_id: str) -> bool:
        return agent_runs.is_active(session_id)


# Single server-side coordinator; it retains no separate mutable run registry.
turn_service = TurnService()
