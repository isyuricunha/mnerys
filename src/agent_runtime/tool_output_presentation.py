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


def completed_tool_round_answer(
    response: str,
    round_texts: Sequence[str],
    tool_events: Sequence[Mapping[str, Any]],
) -> str | None:
    """Select the actual final model round without replaying earlier tool plans.

    The Agent Core retains each round separately for history and also keeps a
    cumulative text accumulator. A terminal answer must use the post-tool
    round, never concatenate pre-tool progress into the answer.  Only select
    a final round when numbered tool receipts prove an earlier round invoked
    tools and its complete text is the suffix of the accumulator. All other
    cases (including tool-only, overwritten or ambiguous finals) stay intact.
    """
    if (
        not isinstance(response, str)
        or not isinstance(round_texts, (list, tuple))
        or not isinstance(tool_events, (list, tuple))
        or not round_texts
    ):
        return None
    final = round_texts[-1]
    if not isinstance(final, str) or not final.strip():
        return None
    numbered = [
        event.get("round")
        for event in tool_events
        if isinstance(event, Mapping) and event.get("tool")
    ]
    if not numbered or any(type(value) is not int or value < 1 for value in numbered):
        # Never ignore an unnumbered tool receipt; it could belong to the
        # actual final round and make the chronology ambiguous.
        return None
    if max(numbered) >= len(round_texts):
        return None
    clean_final = final.strip()
    if not response.strip().endswith(clean_final):
        return None
    # Do not rewrite an already-final answer or a result with no preceding
    # model prose. A complete final round is authoritative in its own right.
    if response.strip() == clean_final:
        return None
    return clean_final


def strip_completed_inspection_preamble(
    response: str,
    tool_events: Sequence[Mapping[str, Any]],
    *,
    user_request: str,
) -> str:
    """Remove an isolated work-plan opener from an already completed read answer.

    This is not a reasoning-channel parser: providers must route explicit
    thinking separately. A visible text preamble can still precede a real
    answer, often without a space after the opening period. Preserve every
    subsequent word and do not strip an opener when no answer follows.
    """
    from src.turn_contract import explicit_local_read_intent

    if (not isinstance(response, str)
            or explicit_local_read_intent(user_request) is None
            or not isinstance(tool_events, (list, tuple))):
        return response

    observed = [
        event for event in tool_events
        if isinstance(event, Mapping) and event.get("tool")
    ]
    if len(observed) < 2 or not any(
        event.get("exit_code") == 0 and not event.get("blocked")
        for event in observed
    ):
        return response
    for event in observed:
        tool = str(event.get("tool"))
        if (tool not in _READ_ONLY_TOOLS or event.get("blocked")
                or event.get("approval_required") or event.get("error")):
            return response
        if tool in {"bash", "host_shell"}:
            from src.command_risk import classify_shell_command
            if not classify_shell_command(str(event.get("command") or "")).is_low_risk:
                return response

    preamble = re.match(
        r"^\s*(?:I['’]ll|I will|Let me)\s+"
        r"(?:(?:work|go) through (?:each|the|these) steps?|"
        r"(?:start|begin) by (?:checking|inspecting|reading|listing)|"
        r"(?:first )?(?:check|inspect|read|list) (?:the|these) files?)"
        r"[^.!?\n]{0,160}[.!?]\s*",
        response,
        re.IGNORECASE,
    )
    if preamble is None:
        return response
    remainder = response[preamble.end():].lstrip()
    if len(remainder) < 50 or re.match(
        r"^(?:I['’]ll|I will|Let me)\s+", remainder, re.I,
    ):
        return response
    return remainder


def summarize_incomplete_local_inspection(
    response: str,
    tool_events: Sequence[Mapping[str, Any]],
    *,
    user_request: str,
) -> str | None:
    """Recover an incomplete explicit read-only inspection from actual receipts.

    Only the terminal answer may be synthesized. Tool events are immutable,
    failed commands remain failed, and a blocked repeat is never called a new
    execution. Unknown/effectful/approval actions abort this fallback.
    """
    from src.turn_contract import explicit_local_read_intent

    if not isinstance(response, str) or not isinstance(tool_events, (tuple, list)):
        return None
    if explicit_local_read_intent(user_request) is None:
        return None
    if not re.search(
        r"\b(?:resultados? reais?|resultado real|c[oó]digos? de sa[ií]da|"
        r"apresente o resultado|informe a branch|informe os tr[eê]s commits)\b",
        user_request, re.I,
    ):
        return None

    def read_only_receipt(event: Mapping[str, Any]) -> bool:
        tool = str(event.get("tool") or "")
        if tool not in _READ_ONLY_TOOLS or type(event.get("exit_code")) is not int:
            return False
        if event.get("blocked") or event.get("approval_required"):
            return False
        if tool in {"bash", "host_shell"}:
            from src.command_risk import classify_shell_command
            return classify_shell_command(str(event.get("command") or "")).is_low_risk
        return True

    receipts: list[Mapping[str, Any]] = []
    for event in tool_events:
        if not isinstance(event, Mapping) or not event.get("tool"):
            continue
        if (
            event.get("blocked")
            and event.get("policy") == "repeated_read_only_call"
            and any(
                previous.get("tool") == event.get("tool")
                and previous.get("command") == event.get("command")
                and previous.get("exit_code") == 0
                for previous in receipts
            )
        ):
            continue
        if not read_only_receipt(event):
            return None
        receipts.append(event)
    if not 2 <= len(receipts) <= 8:
        return None

    answer = response.strip()
    # Existing terminal summarizers may wrap a single command's stdout
    # in a plaintext Markdown fence; this is still only one tool's result.
    fenced = re.fullmatch(r"```(?:text|plaintext)?\n([\s\S]*?)\n```", answer)
    if fenced:
        answer = fenced.group(1).strip()
    if not answer:
        incomplete = True
    elif re.search(
        r"Blocked an exact repeat of a successful read-only call|"
        r"The exact operation is outside server request authority",
        answer, re.I,
    ):
        incomplete = True
    else:
        # A provider that returns only the last stdout (e.g. "ubuntu") has
        # not answered a request to report several independently run commands.
        incomplete = any(
            answer == str(e.get("output") or e.get("stdout") or "").strip()
            for e in receipts
        )
    if not incomplete:
        return None

    lines = ["Resultados observados das ferramentas:", ""]
    for index, event in enumerate(receipts, 1):
        tool = str(event["tool"])
        command = str(event.get("command") or "").strip()
        # Never interpolate arbitrary shell input or resource payload as a
        # Markdown label. The full command is still visible on its own card.
        label = (
            command if tool in {"bash", "host_shell"}
            and len(command) <= 80
            and re.fullmatch(r"[A-Za-z0-9_./~ -]+", command)
            else tool
        )
        lines.append(f"{index}. `{label}`: exit `{event['exit_code']}`")
        output = str(
            event.get("output") or event.get("stderr")
            or event.get("stdout") or event.get("error") or ""
        ).strip()
        if output:
            short = output.replace("\r", "").splitlines()
            if len(short) <= 4 and len(output) <= 640:
                # Short multi-line output (e.g. three git commits) is evidence
                # the user explicitly requested; do not hide it behind the
                # long-output placeholder. Indentation preserves lines as
                # literal Markdown code instead of interpreting their content.
                lines.extend("    " + line for line in short)
            else:
                lines.append("   Saída completa disponível na etapa da ferramenta.")
    return "\n".join(lines)
