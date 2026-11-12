"""Presentation-only treatment of redundant tool-output echoes.

The authoritative results remain attached to their tool events.  This module
never executes tools, changes effects, guesses missing output, or rewrites
general model answers.  It only condenses an overwhelmingly duplicated,
lengthy read-only result shown in the timeline into an observed receipt list.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import re
from typing import Any

_READ_ONLY_TOOLS = frozenset({
    "bash", "host_shell", "read_file", "ls", "glob", "grep",
    "get_workspace",
})
_MIN_CHARS = 1200
_MIN_LINES = 12


def _nonempty_lines(value: str) -> list[str]:
    return [" ".join(line.split()) for line in value.splitlines() if line.strip()]


def _is_large_duplicate(response: str, output: str) -> bool:
    """Require near-whole-document, bidirectional line overlap, never a quote."""
    if min(len(response), len(output)) < _MIN_CHARS:
        return False
    answer = _nonempty_lines(response)
    source = _nonempty_lines(output)
    if min(len(answer), len(source)) < _MIN_LINES:
        return False
    a, b = Counter(answer), Counter(source)
    common = sum(min(count, b.get(line, 0)) for line, count in a.items())
    return common / len(answer) >= 0.86 and common / len(source) >= 0.82


def summarize_repeated_tool_output(
    response: str,
    tool_events: Sequence[Mapping[str, Any]],
    *,
    user_request: str = "",
) -> str | None:
    """Return receipt-only text only for an unambiguous multi-tool echo.

    No output is taken away from the user: tool events already contain the
    complete, authorized stdout/stderr for the expandable timeline.  This
    fallback is not a replacement for proper final-answer synthesis.
    """
    if not isinstance(response, str) or not isinstance(tool_events, (tuple, list)):
        return None
    # An explicit request for the complete literal output outranks automatic
    # deduplication. This checks the user's instruction, never source data.
    if re.search(
        r"\b(?:verbatim|full contents|complete output|entire output|"
        r"texto completo|conteúdo integral|saída completa|na íntegra)\b",
        str(user_request or ""), re.IGNORECASE,
    ):
        return None

    def safe_receipt(event: Mapping[str, Any]) -> bool:
        name = str(event.get("tool") or "")
        if name not in _READ_ONLY_TOOLS or type(event.get("exit_code")) is not int:
            return False
        if event.get("blocked") or event.get("approval_required"):
            return False
        if name in {"bash", "host_shell"}:
            from src.command_risk import classify_shell_command

            return classify_shell_command(str(event.get("command") or "")).is_low_risk
        return True

    observed = [
        event for event in tool_events
        if isinstance(event, Mapping) and event.get("tool")
    ]
    # If the turn also has an effect, unknown tool, or unconfirmed receipt,
    # never replace the model's answer with a read-only completion notice.
    if len(observed) < 2 or not all(safe_receipt(event) for event in observed):
        return None
    receipts = observed
    if not any(
        _is_large_duplicate(response, str(event.get("output") or ""))
        for event in receipts
    ):
        return None

    lines = ["The full command outputs are preserved in the tool steps above.", ""]
    for index, event in enumerate(receipts, 1):
        name = str(event["tool"])
        code = event["exit_code"]
        # A short, simple read-only command is useful context. Never render
        # shell metacharacters, long arguments, or raw output in this notice.
        cmd = str(event.get("command") or "").strip()
        simple = (
            name in {"bash", "host_shell"}
            and len(cmd) <= 72
            and re.fullmatch(r"[A-Za-z0-9_./~ -]+", cmd) is not None
        )
        label = f"`{cmd}`" if simple else f"`{name}`"
        lines.append(f"- Tool {index} ({label}): exit code `{code}`")
    return "\n".join(lines)
