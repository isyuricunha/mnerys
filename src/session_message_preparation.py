"""Shared, lightweight preparation of persisted session message payloads.

The current legacy SessionManager and background-job delivery use this exact
serialization boundary. The future (currently unactivated) fenced writer also
accepts the same immutable prepared payload. No DB/HTTP client is imported.

This function does NOT check owner, authorize a session, reserve uploads,
sanitize tool approvals, or commit messages. Callers must retain their existing
validated upload reservation and session ownership gates before invoking it.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

from src.attachment_refs import persistable_message_content


@dataclass(frozen=True)
class PreparedSessionMessage:
    """Text and serialized metadata ready for an admitted persistence writer.

    This is a server-internal value object, not an API request or proof that
    attachment ownership, client authorization, or tool execution were checked.
    """

    role: str
    content: str
    metadata_json: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Message role is required")
        if not isinstance(self.content, str):
            raise ValueError("Prepared message content must be text")
        if self.metadata_json is not None and not isinstance(self.metadata_json, str):
            raise ValueError("Serialized metadata must be text or None")


def prepare_session_message(
    role: str, content: Any, metadata: Optional[dict] = None,
) -> PreparedSessionMessage:
    """Preserve existing chat storage serialization, with no input mutation.

    Keep multimodal provider bytes out of persisted message content; retain
    the existing JSON metadata encoding exactly (including default separators
    and ASCII-escaped unicode). A caller that needs upload retention must
    reserve its attachments *before* calling this function.
    """
    return PreparedSessionMessage(
        role=role,
        content=persistable_message_content(content, metadata),
        metadata_json=json.dumps(metadata) if metadata else None,
    )
