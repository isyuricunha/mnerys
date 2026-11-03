"""Transport-independent data contracts for one assembled chat context.

These are in-memory result containers; no durable Gateway contract is implied.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class PresetInfo:
    """Extracted preset parameters."""
    temperature: Optional[float]
    max_tokens: Optional[int]
    system_prompt: Optional[str]
    character_name: Optional[str]
    persona_memory: Optional[str] = None
    persona_memory_schema: str = "general"


@dataclass
class PreprocessedMessage:
    """Result of chat_handler.preprocess_message."""
    enhanced_message: str
    user_content: Any  # str or list (multimodal)
    text_for_context: str
    youtube_transcripts: list
    attachment_meta: list


@dataclass
class ChatContext:
    """Everything needed to call the LLM after context-building."""
    preface: list
    rag_sources: list
    web_sources: list
    used_memories: list
    messages: list
    context_length: int
    was_compacted: bool
    user: Optional[str]
    uprefs: dict
    preset: PresetInfo
    preprocessed: PreprocessedMessage
    context_trimmed: bool = False
    context_messages_before_trim: int = 0
    context_messages_after_trim: int = 0
    context_tokens_before_trim: int = 0
    context_tokens_after_trim: int = 0
    # Documents auto-created server-side during preprocess (e.g. when an
    # attached fillable PDF gets rendered into a markdown editor doc).
    # The chat route emits a doc_update SSE event for each before streaming
    # begins, so the editor pane switches to the new doc immediately.
    auto_opened_docs: list = field(default_factory=list)
    # Uploads attached to this user turn, resolved and owner-checked for the
    # agent's private context. This is not emitted to the browser.
    uploaded_files: list = field(default_factory=list)
    # Route-neutral prompt before any model-window compaction/trimming. This is
    # retained only when explicit foreground fallbacks are enabled so each
    # concrete candidate can apply its own context budget independently.
    route_messages: list = field(default_factory=list)
