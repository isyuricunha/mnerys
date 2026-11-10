"""Metadata-only journal for admitted Web streams. No authority is restored."""
from contextlib import aclosing
from datetime import datetime, timezone
import logging
from uuid import uuid4

from src.agent_tool_events import observe_tool_events
from src.tool_approvals import observe_approval_proposals

logger = logging.getLogger(__name__)


class WebRunJournal:
    def __init__(self, records, *, owner, session_id, incarnation_id):
        self.records = records
        self.owner, self.session_id, self.incarnation_id = owner, session_id, incarnation_id
        self.turn_id = uuid4().hex
        self.approval_id = None
        self.terminal = False
        self.failed = False
        records.open_turn(owner=owner, session_id=session_id, incarnation_id=incarnation_id,
                          turn_id=self.turn_id, policy_version="web-recovery-v1")

    def _append(self, kind, payload):
        self.records.append_web_event(turn_id=self.turn_id, owner=self.owner, kind=kind, payload=payload)

    def _tool(self, event):
        if not self.terminal and event.type != "tool.progress":
            self._append(event.type, {"tool": event.tool, "round": event.round, "exit_code": event.exit_code})

    def _proposal(self, pending, _superseded):
        if pending.owner != self.owner.strip().casefold() or pending.session_id != self.session_id:
            return  # Nested runs cannot contribute another session's seal.
        self.records.record_approval(
            approval_id=pending.approval_id, owner=self.owner, session_id=self.session_id,
            incarnation_id=self.incarnation_id, turn_id=self.turn_id,
            tool_name=pending.tool_name, operation_digest=pending.digest,
            resource_identity=None, effective_authority=None,
            expires_at=datetime.fromtimestamp(pending.expires_at, tz=timezone.utc),
            web_projection=True,
        )
        self.approval_id = pending.approval_id

    def _finish(self, status, kind, payload):
        if not self.terminal:
            self.records.finalize_with_event(turn_id=self.turn_id, owner=self.owner,
                                             status=status, kind=kind, payload=payload)
            self.terminal = True

    def drained(self, status):
        """Covers cancellation before the wrapped generator starts."""
        try:
            if status == "stopped":
                self._finish("cancelled", "turn.cancelled", {"effects": "unknown"})
            else:
                self._finish("failed", "turn.failed", {
                    "code": "incomplete_stream", "effects": "unknown", "retryable": False,
                })
        except Exception:
            logger.warning("Could not persist Web terminal; restart reconciliation remains available")

    async def wrap(self, stream):
        with observe_tool_events(self._tool), observe_approval_proposals(self._proposal):
            async with aclosing(stream):
                async for frame in stream:
                    if frame.startswith("event: error"):
                        self.failed = True
                    if frame.strip() == "data: [DONE]":
                        if self.failed:
                            self._finish("failed", "turn.failed", {
                                "code": "stream_error", "effects": "unknown", "retryable": False,
                            })
                        elif self.approval_id:
                            self._finish("awaiting_approval", "turn.awaiting_approval",
                                         {"approval_id": self.approval_id})
                        else:
                            self._finish("completed", "turn.completed", {})
                    yield frame
