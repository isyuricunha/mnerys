"""Pure context-selection rules shared across trusted Mnerys transports.

No HTTP, database, model provider or file access belongs in this module.
"""
import re
from typing import Any

_CASUAL_OPENING_RE = re.compile(
    r"^\s*(?:h+i+|hey+|hello+|yo+|sup+|what'?s up|wass?up|hiya|howdy|"
    r"lol|lmao|haha+|hehe+|thanks?|thank you|ty|idk|dunno|meh|bruh|bro)\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_CASUAL_BLOCKLIST_RE = re.compile(
    r"\b(?:cookbook|serve|serving|launch|start|vllm|sglang|llama\.?cpp|ollama|"
    r"download|model|email|document|doc|note|calendar|task|search|web|research|"
    r"file|folder|repo|git|settings?|endpoint|api|token|mcp)\b",
    re.IGNORECASE,
)
_PERSONAL_TOOL_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"email|emails|mail|inbox|gmail|"
    r"calendar|events?|meetings?|appointments?|schedule|"
    r"notes?|todo|checklist|reminders?|tasks?"
    r")\b",
    re.IGNORECASE,
)


def _is_casual_low_signal(text: str) -> bool:
    """Short greetings/slang should not pull memory, skills, RAG, or docs."""
    s = str(text or "").strip()
    m = _CASUAL_OPENING_RE.match(s)
    if not m:
        return False
    tail = m.group("tail") or ""
    if _CASUAL_BLOCKLIST_RE.search(tail):
        return False
    tail_words = re.findall(r"[A-Za-z0-9_'-]+", tail)
    return len(tail_words) <= 2


def _truthy_request_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _session_is_research_spinoff(sess) -> bool:
    """True if this session was created via research "Discuss" spin-off.

    Detected by the primer system message the spin-off endpoint seeds into
    history (metadata ``research_spinoff_from``). Such sessions are grounded
    on the seeded report, so global memory + personal-doc RAG injection is
    suppressed for them (the report is the sole knowledge base). Handles both
    ChatMessage objects and plain dicts.
    """
    for m in getattr(sess, "history", []) or []:
        role = getattr(m, "role", None)
        if role is None and isinstance(m, dict):
            role = m.get("role")
        if role != "system":
            continue
        md = getattr(m, "metadata", None)
        if md is None and isinstance(m, dict):
            md = m.get("metadata")
        if (md or {}).get("research_spinoff_from"):
            return True
    return False
