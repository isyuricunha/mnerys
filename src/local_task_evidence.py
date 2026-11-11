"""Evidence contract for local directory answers after tool execution.

Only server-owned execution receipts establish a completed filesystem read.
Model prose, web snippets, and a failed or rejected tool invocation cannot be
promoted to verified directory contents.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.turn_contract import local_filesystem_intent

_LOCAL_READ_TOOLS = frozenset({
    "bash", "host_shell", "ls", "get_workspace", "glob", "grep", "read_file",
    "python",
})


def failed_local_listing_notice(
    latest_user_text: str,
    history: Iterable[Mapping[str, Any]],
    tool_events: Iterable[Mapping[str, Any]],
) -> str | None:
    """Return a factual failure notice only when *all* relevant reads failed.

    This does not replace a successful tool's output, previous known history,
    or general model synthesis. It prevents a model from making up new file
    names after the Gateway has refused every attempted local inspection.
    """
    if local_filesystem_intent(latest_user_text, history) != "list":
        return None

    attempted = []
    for event in tool_events:
        if not isinstance(event, Mapping):
            continue
        tool = str(event.get("tool") or "").strip().lower()
        if tool in _LOCAL_READ_TOOLS:
            attempted.append(event)
    if not attempted:
        return None

    for event in attempted:
        exit_code = event.get("exit_code")
        if (
            event.get("blocked") is not True
            and event.get("execution_attempted") is True
            and exit_code == 0
        ):
            return None

    return (
        "Não consegui verificar o conteúdo solicitado: as ferramentas de "
        "inspeção falharam ou foram bloqueadas pelo Gateway. "
        "Nenhuma nova listagem foi confirmada; não vou inventar arquivos."
    )
