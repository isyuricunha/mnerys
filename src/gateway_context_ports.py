"""M2.1 dependency ports for assembling an authorized chat context.

This is a structural interface, not an extra engine or a public transport API.
The current Web adapter binds the inherited implementations; independent
Gateway/CLI/TUI adapters can provide compatible services in later milestones.

The data read/write ordering belongs to the shared context algorithm. Ports
do not grant privileges; the trusted caller must first validate session
ownership and derive the owner from the issued Gateway admission.
"""

from typing import Any, Protocol


class ChatContextPorts(Protocol):
    """The I/O seams consumed by the shared context builder."""

    def extract_preset(self, chat_handler: Any, preset_id: Any) -> Any: ...

    async def preprocess_message(
        self,
        chat_handler: Any,
        message: str,
        attachment_ids: list,
        session: Any,
        *,
        auto_opened_docs: list,
        allow_tool_preprocessing: bool,
        allow_attachment_preprocessing: bool = False,
    ) -> Any: ...

    def append_user_message(
        self,
        session: Any,
        chat_handler: Any,
        preprocessed: Any,
        *,
        incognito: bool,
        interaction_mode: str | None,
        auto_escalated: bool,
    ) -> None: ...

    def append_private_message(
        self, session_id: str, role: str, content: Any, metadata: dict,
    ) -> None: ...

    def private_messages(self, session_id: str) -> list[dict[str, Any]]: ...

    def load_preferences(self, owner: str | None) -> dict: ...

    def upload_manifest(
        self, attachment_ids: list, upload_handler: Any, owner: str | None,
    ) -> list[dict]: ...

    def build_preface(self, chat_processor: Any, **options: Any) -> tuple: ...

    def used_memories(self, chat_processor: Any) -> list: ...

    async def compact(
        self,
        session: Any,
        endpoint_url: str,
        model: str,
        messages: list,
        headers: Any,
        *,
        owner: str | None,
        **options: Any,
    ) -> tuple: ...

    def trim(self, messages: list, context_length: int) -> list: ...

    def youtube_sources(self, message: str, transcripts: list) -> list[dict]: ...

    def normalize_model(self, session: Any) -> str | None: ...

    def session_history(self, session: Any) -> list: ...

    def current_datetime_message(self) -> dict: ...

    def context_length(self, endpoint_url: str, model: str) -> int: ...

    def estimate_tokens(self, messages: list) -> int: ...
