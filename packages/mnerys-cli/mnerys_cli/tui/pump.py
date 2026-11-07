"""The turn event pump: cursor-based polling with bounded reconnection.

The Gateway v1 event endpoint replays the current ordered buffer after an
``after_seq`` cursor and closes each response with ``[DONE]``. The pump
therefore polls: it applies every event through the tracker exactly once,
sleeps between rounds, reconnects with the same cursor after transport
failures and stops on the first terminal state.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mnerys_cli.errors import MnerysCliError
from mnerys_cli.tui.events import TERMINAL_STATUSES, TurnTracker

logger = logging.getLogger("mnerys.tui.pump")

EventCallback = Callable[[str], Awaitable[None]]


class TurnPump:
    """Drive one turn's events into a tracker until a terminal state."""

    def __init__(
        self, client, turn_id: str, session_id: str, tracker: TurnTracker,
        on_event: EventCallback, *,
        poll_interval: float = 0.4, max_backoff: float = 5.0,
        max_consecutive_failures: int = 5, max_rounds: int = 1200,
    ):
        self.client = client
        self.turn_id = turn_id
        self.session_id = session_id
        self.tracker = tracker
        self.on_event = on_event
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.max_consecutive_failures = max_consecutive_failures
        self.max_rounds = max_rounds
        self._cancel_requested = False
        self.consecutive_failures = 0

    def request_cancel(self) -> None:
        """Ask for a clean local stop of this pump (the server cancel is the
        authority; the pump just refuses to keep waiting afterwards)."""
        self._cancel_requested = True

    async def run(self) -> str:
        """Poll until terminal; returns the final tracker status.

        Every return path leaves ``tracker.status`` consistent with the
        returned value, so the UI never renders a stale streaming state.
        """
        for _round in range(self.max_rounds):
            if self._cancel_requested:
                self.tracker.status = "cancelled"
                return "cancelled"
            try:
                async for item in self.client.turn_events(
                    self.turn_id, after_seq=self.tracker.applied_sequence,
                ):
                    kind = self.tracker.apply(item)
                    if kind is not None:
                        await self.on_event(kind)
                    if self.tracker.status in TERMINAL_STATUSES:
                        return self.tracker.status
                self.consecutive_failures = 0
            except MnerysCliError as exc:
                self.consecutive_failures += 1
                if self.consecutive_failures > self.max_consecutive_failures:
                    logger.warning(
                        "Event pump gave up after %s failures",
                        self.consecutive_failures,
                    )
                    self.tracker.status = "failed"
                    raise MnerysCliError(
                        f"lost the Gateway while streaming turn {self.turn_id}: {exc}",
                    ) from exc
                backoff = min(
                    self.poll_interval * (2 ** self.consecutive_failures),
                    self.max_backoff,
                )
                await asyncio.sleep(backoff)
                continue
            except Exception as exc:  # never crash the app on transport surprises
                self.consecutive_failures += 1
                if self.consecutive_failures > self.max_consecutive_failures:
                    logger.exception(
                        "Event pump gave up after an unexpected stream error",
                    )
                    self.tracker.status = "failed"
                    raise MnerysCliError(
                        "lost the Gateway while streaming turn "
                        f"{self.turn_id}: unexpected stream error",
                    ) from exc
                await asyncio.sleep(
                    min(
                        self.poll_interval * (2 ** self.consecutive_failures),
                        self.max_backoff,
                    ),
                )
                continue
            if self.tracker.status in TERMINAL_STATUSES:
                return self.tracker.status
            await asyncio.sleep(self.poll_interval)
        # Bounded rounds exhausted: confirm through the snapshot instead of
        # inventing a state; the snapshot is the server's truth.
        try:
            snapshot = await self.client.turn_snapshot(self.turn_id)
        except MnerysCliError:
            snapshot = {}
        status = str(snapshot.get("status") or "failed")
        if status not in TERMINAL_STATUSES:
            status = "failed"
        self.tracker.status = status
        return status
