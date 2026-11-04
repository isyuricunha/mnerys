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
from src.agent_tool_events import bind_native_tool_events
from src.agent_loop import stream_agent_loop
from src.tool_execution import bind_execution_bridge
from src.turn_contract import bind_turn_contract


@dataclass(frozen=True)
class TurnStream:
    """An internal run handle; events still contain legacy SSE frames."""

    run_id: str
    events: AsyncIterator[str]


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
        with bind_native_tool_events(native_tools), bind_turn_contract(kwargs.get("turn_contract")):
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
        self, session_id: str, stream: AsyncGenerator[str, None]
    ) -> TurnStream:
        """Retain legacy per-session replacement, persistence and SSE replay."""
        run = agent_runs.start(session_id, stream)
        return TurnStream(
            run_id=run.run_id,
            events=agent_runs.subscribe(session_id, run),
        )

    def resume_active(self, session_id: str) -> Optional[TurnStream]:
        """Subscribe to the exact active run; do not switch on replacement."""
        run = agent_runs.get_active_run(session_id)
        if run is None:
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
