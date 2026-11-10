# core/models.py
"""
Pure data models — no database logic, no side effects.

These are simple datacontainers. All persistence is handled by SessionManager.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, TYPE_CHECKING

from src.tool_approval_scopes import (
    CHAT_SESSION_APPROVAL_CONTEXT_MARKER,
)

if TYPE_CHECKING:
    from .session_manager import SessionManager

# Module-level session manager singleton (single source of truth)
_SESSION_MANAGER_INSTANCE: Optional["SessionManager"] = None


def set_session_manager_instance(manager: "SessionManager"):
    """Set the global SessionManager singleton."""
    global _SESSION_MANAGER_INSTANCE
    _SESSION_MANAGER_INSTANCE = manager


def get_session_manager_instance() -> Optional["SessionManager"]:
    """Get the global SessionManager singleton."""
    return _SESSION_MANAGER_INSTANCE


# Keep legacy name for backward compatibility
set_session_manager = set_session_manager_instance
get_session_manager = get_session_manager_instance


@dataclass
class ChatMessage:
    """A single chat message."""
    role: str
    content: str
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for API responses."""
        result = {"role": self.role, "content": self.content}
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    def get(self, key: str, default=None):
        """Dict-like access for compatibility."""
        return getattr(self, key, default)


@dataclass
class Session:
    """A chat session — pure data container.

    ``.history`` is the authoritative mutable message list. Callers may
    read, append, pop, or reassign it directly — these changes take
    effect immediately. ``_history`` remains a compatibility alias that
    always resolves to the authoritative ``history`` list.

    Each session gets its own unique history list at construction time
    (the dataclass default is never shared between instances).
    """

    id: str
    name: str
    endpoint_url: str
    model: str
    rag: bool = False
    archived: bool = False
    headers: Optional[Dict[str, str]] = None
    history: List[ChatMessage] = None
    owner: Optional[str] = None
    is_important: bool = False
    message_count: int = 0
    memory_extraction_enabled: bool = True
    memory_injection_enabled: bool = True
    skill_injection_enabled: bool = True
    thinking_mode: str = "off"
    temperature_override: Optional[float] = None
    max_tokens_override: Optional[int] = None
    cwd: Optional[str] = None
    # Registered ModelEndpoint id this session is bound to (None = legacy /
    # URL-matched). Lets two endpoints that share a provider URL but not
    # credentials stay distinguishable.
    endpoint_id: Optional[str] = None
    # M2.2 write fencing: the persisted revision/incarnation of this
    # session lifetime. None on unmigrated rows; guarded writers fail
    # closed rather than guess when these are missing.
    revision: Optional[int] = None
    incarnation_id: Optional[str] = None
    _approval_binding: tuple | None = field(default=None, init=False, repr=False, compare=False)

    def grant_chat_session_approval(self) -> None:
        """Remember an admitted chat-scope decision on this live session only."""
        self._approval_binding = (self.id, self.owner, self.incarnation_id)

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}
        # Ensure each session gets its OWN list (not the shared dataclass default)
        if self.history is None:
            self.history = []

    @property
    def _history(self) -> List[ChatMessage]:
        """Compatibility alias for callers that still reference ``_history``."""
        return self.history

    @_history.setter
    def _history(self, messages: List[ChatMessage]):
        self.history = messages

    def add_message(self, message: ChatMessage):
        """
        Add a message to this session.

        Appends to the authoritative history list and increments
        message_count. Delegates to SessionManager for persistence
        if available.
        """
        self.history.append(message)
        self.message_count = len(self.history)

        # Only the exact Session instance registered with the process-wide
        # manager may persist through it. Unmanaged/standalone objects must
        # remain in-memory even when another session manager is initialized;
        # a stale object sharing an ID must not write under a different one.
        manager = _SESSION_MANAGER_INSTANCE
        if (
            manager is not None
            and getattr(manager, "sessions", {}).get(self.id) is self
            and manager._persist_message(self.id, message) is False
        ):
            self._discard_failed_message(message)

    def _discard_failed_message(self, message: ChatMessage) -> None:
        """Remove only this uncommitted append, preserving later messages.

        Look up by identity rather than value to avoid removing a different
        message with the same content, and mutate the existing history list
        so anyone holding a reference sees the rollback.
        """
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index] is message:
                del self.history[index]
                break
        self.message_count = len(self.history)

    def get_context_messages(self) -> List[Dict[str, Any]]:
        """Get messages in format for LLM API.

        Slash-command / setup replies are persisted to history so they render
        in the transcript, but they are UI chatter (e.g. ``/setup ...`` and its
        status lines) the user never meant as conversation. They carry
        ``metadata.source == "slash"``; exclude them here so they never reach
        the model. Display/history-load paths use the raw ``history`` and are
        unaffected.
        """
        messages = [
            msg.to_dict()
            for msg in self.history
            if (msg.metadata or {}).get("source") != "slash"
            # The `hidden` flag has two meanings: compaction summaries are
            # hidden from DISPLAY but stay in model context; repaired
            # duplicate rows (a specific reason) never reach the model.
            and not (
                (msg.metadata or {}).get("hidden")
                and (msg.metadata or {}).get("hidden_reason")
                == "duplicate-write-repair"
            )
        ]
        from src.background_tool_jobs import background_result_context
        messages = [part for message in messages for part in (
            *background_result_context(message.get('metadata')), message,
        )]
        # Resume an interrupted thinking-only response from its actual model
        # reasoning channel. Restrict this to the latest assistant message so
        # old traces do not accumulate in context or cause reasoning loops.
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata") or {}
            thinking = str(metadata.get("thinking") or "").strip()
            if metadata.get("stopped") and thinking:
                resumed = dict(message)
                resumed["reasoning_content"] = thinking
                messages[index] = resumed
            break
        # A transcript, including a legacy signed card or a marker in copied
        # metadata, cannot reconstitute this process-local decision.
        for index, message in enumerate(messages):
            metadata = message.get("metadata")
            if isinstance(metadata, dict) and CHAT_SESSION_APPROVAL_CONTEXT_MARKER in metadata:
                clean = dict(metadata)
                clean.pop(CHAT_SESSION_APPROVAL_CONTEXT_MARKER)
                messages[index] = {**message, "metadata": clean}
        if self._approval_binding != (self.id, self.owner, self.incarnation_id):
            return messages

        # Keep the grant close to the latest user request so route-neutral
        # compaction/trimming preserves it. Copy the metadata instead of
        # mutating the durable transcript object.
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") != "user":
                continue
            message = dict(messages[index])
            metadata = dict(message.get("metadata") or {})
            metadata[CHAT_SESSION_APPROVAL_CONTEXT_MARKER] = True
            message["metadata"] = metadata
            messages[index] = message
            break
        return messages

    def get(self, key: str, default=None):
        """Dict-like access for compatibility."""
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        """Allow session['field'] syntax."""
        return getattr(self, key)
