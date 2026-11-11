"""M5.3 increment 3: the deterministic, local command-risk classifier.

One shared, Gateway-owned risk classification boundary for shell/process
execution. It is local, deterministic, provider-independent and
transport-independent; it runs inside the existing approval gate
(``ToolRunSecurityContext.decision_for``) immediately before the approval flow
and process launch, so every execution surface (Web, CLI, TUI, MCP
continuations, background jobs) is covered — not just one tool spelling.

Classification NEVER grants execution authority:

* ``LOW_RISK`` — a concrete, confidently understood read-only operation. It
  does not require an ordinary risk or external-context confirmation, but
  admission, tool policy, request authority, resource/CWD binding, sensitive
  path protection, read-before-edit and artifact publication still apply.
* ``REQUIRES_APPROVAL`` — potentially destructive, sensitive, privileged, or
  insufficiently understood. Flows into the existing four-decision approval
  flow with the concrete risk reason; the exact-operation scoped rules
  (``src/tool_approval_rules.py``) can suppress the repeat prompt only for the
  exact reviewed operation.
* ``DENIED`` — a non-delegable prohibition (delegated credentials and other
  explicit boundaries); no approval can lift it.

Parsing honesty: ``shlex`` performs lexical analysis, not shell interpretation.
The splitter is quote-aware and conservative — unbalanced quotes, unknown
constructs, or any dynamically evaluated content (command substitution,
variable-expanded destructive arguments, heredoc bodies, script files) classify
as REQUIRES_APPROVAL rather than guessing safety. Nothing is ever executed to
determine its risk.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "COMMAND_RISK_VERSION",
    "RiskCategory",
    "CommandRiskClassification",
    "classify_shell_command",
    "classify_python_code",
]

COMMAND_RISK_VERSION = 1

LOW_RISK = "low_risk"
REQUIRES_APPROVAL = "requires_approval"
DENIED = "denied"


@dataclass(frozen=True)
class CommandRiskClassification:
    category: str  # LOW_RISK | REQUIRES_APPROVAL | DENIED
    level: str  # informational | low | moderate | high | critical
    reason: str
    matched: str = ""  # the matched command segment / pattern
    scope: str = ""  # relevant resource scope (path or workspace)
    analysis_complete: bool = True
    requires_approval: bool = False
    classifier_version: int = COMMAND_RISK_VERSION

    @property
    def is_denied(self) -> bool:
        return self.category == DENIED

    @property
    def is_low_risk(self) -> bool:
        return self.category == LOW_RISK


def _approval(level: str, reason: str, matched: str = "", scope: str = "",
              complete: bool = True) -> CommandRiskClassification:
    return CommandRiskClassification(
        REQUIRES_APPROVAL, level, reason, matched=matched, scope=scope,
        analysis_complete=complete, requires_approval=True,
    )


def _low(reason: str, matched: str = "") -> CommandRiskClassification:
    return CommandRiskClassification(LOW_RISK, "low", reason, matched=matched)


# ── Quote-aware top-level splitting ───────────────────────────────────────

_QUOTE_CHARS = {"'", '"'}


def _split_top_level(command: str, separators: tuple[str, ...]):
    """Split on the given shell control operators OUTSIDE quotes/substitutions.

    Returns (segments, analysis_complete). Any structural uncertainty
    (unbalanced quotes, unterminated escapes) reports incomplete analysis so
    the caller classifies conservatively.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote:
            if quote == '"' and char == "\\" and index + 1 < length:
                current.append(command[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            current.append(char)
            index += 1
            continue
        if char in _QUOTE_CHARS:
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            current.append(command[index:index + 2])
            index += 2
            continue
        # A subshell/command substitution is a nested evaluation boundary.
        if char == "(" and (current and current[-1] == "$" or _last_word(current)):
            close = _find_matching_paren(command, index)
            if close is None:
                return [], False
            current.append(command[index:close + 1])
            index = close + 1
            continue
        two = command[index:index + 2]
        if "&&" in separators and two == "&&":
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if "||" in separators and two == "||":
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if char in separators and char in (";", "|", "\n", "&"):
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quote is not None:
        return [], False  # unbalanced quote: cannot analyze reliably
    segments.append("".join(current))
    return segments, True


def _find_matching_paren(text: str, open_index: int) -> Optional[int]:
    depth = 0
    quote: Optional[str] = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in _QUOTE_CHARS:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _last_word(chars: list[str]) -> bool:
    stripped = "".join(chars).rstrip()
    if not stripped:
        return False
    last = stripped.split()[-1] if stripped.split() else ""
    return bool(last) and last.isidentifier() or last in {";", "&"}


# ── Risk taxonomy ─────────────────────────────────────────────────────────

SENSITIVE_PATH_MARKERS = (
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".netrc", ".env",
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials", ".password",
    "shadow", "sudoers", "authorized_keys", ".git-credentials",
)

# Read-only, confidently understood executables (inspection and output only).
_SAFE_READ_ONLY = {
    "ls", "cat", "head", "tail", "pwd", "echo", "printf", "whoami", "date",
    "which", "file", "stat", "wc", "sort", "uniq", "grep", "rg", "diff",
    "du", "df", "free", "uname", "hostname", "printenv", "env", "id",
    "groups", "tree", "true", "false", "test", "sleep", "jq", "column",
    "basename", "dirname", "realpath", "readlink", "md5sum", "sha256sum",
    "cksum", "tty", "seq",
}

# These lists contain only inspection subcommands. Mutating operations need
# ordinary approval even when a command starts with a familiar executable.
_GIT_SAFE_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "rev-parse", "describe", "blame",
    "ls-files", "shortlog",
})
_DOCKER_SAFE_SUBCOMMANDS = frozenset({
    "ps", "images", "logs", "version", "info", "inspect", "stats",
})


def _basename(executable: str) -> str:
    return executable.rstrip("/").rsplit("/", 1)[-1].lower()


def _argv_of(segment: str) -> Optional[list[str]]:
    """Lexically split one pipeline stage into argv (shlex, POSIX mode).

    ``shlex`` is lexical only; failure or unbalanced input returns None and
    the caller classifies conservatively.
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return None
    return tokens or None


def _contains_substitution(segment: str) -> bool:
    stripped = _strip_quoted_parts(segment)
    return "$(" in stripped or "`" in stripped or "<(" in stripped


def _contains_variable_expansion(segment: str) -> bool:
    stripped = _strip_quoted_parts(segment)
    return "$" in stripped


def _strip_quoted_parts(segment: str) -> str:
    """Remove quoted literal text so syntax analysis sees real operators only.

    Single-quoted text is literal (stripped entirely). DOUBLE-quoted text is
    kept with its escapes removed — the shell still expands substitutions and
    variables inside it, so it must remain visible to the dynamic-content
    detection.
    """
    out: list[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(segment):
                out.append(segment[index + 1])
                index += 2
                continue
            if char == '"':
                quote = None
            out.append(char)
            index += 1
            continue
        if char in _QUOTE_CHARS:
            quote = char
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _mentions_sensitive_path(segment: str) -> Optional[str]:
    lowered = segment.lower()
    for marker in SENSITIVE_PATH_MARKERS:
        if marker in lowered:
            return marker
    return None


def _targets_root_or_wildcard(argv: list[str]) -> bool:
    for token in argv[1:]:
        if token in {"/", "/*", "/*/", "..", "../..", "~", "~/", "~/*"}:
            return True
        if token.startswith("/") and token.count("/") <= 2 and "*" in token:
            return True
    return False


def _classify_simple_command(argv: list[str], segment: str) -> CommandRiskClassification:
    executable = _basename(argv[0])
    sensitive = _mentions_sensitive_path(segment)
    dynamic = _contains_substitution(segment)

    # Wrappers: elevate or recurse into the wrapped payload.
    if executable in {"sudo", "doas", "pkexec"}:
        return _approval(
            "high", "Privilege escalation (sudo) requires explicit approval.",
            matched=segment,
        )
    if executable in {"sh", "bash", "dash", "zsh", "ksh"} and len(argv) > 1 and argv[1] in ("-c",):
        # Nested shell execution: classify the lexed payload token (shlex
        # already removed the quoting).
        inner = argv[2] if len(argv) > 2 else ""
        return classify_shell_command(inner) if inner.strip() else _low(
            "Empty nested shell command.", matched=segment,
        )
    if executable in {"sh", "bash", "dash", "zsh", "ksh"} and len(argv) > 1:
        return _approval(
            "moderate",
            "Script-file execution runs uninspected code and requires approval.",
            matched=segment,
        )
    if executable in {"env", "command", "nohup", "stdbuf", "timeout", "nice", "setsid"} and len(argv) > 1:
        return _classify_simple_command(argv[1:], segment)
    if executable == "cd":
        # A literal directory change only affects the current shell's CWD.
        # This is a risk hint, never a workspace grant: the existing runner
        # must still enforce session CWD, resource authority and confinement.
        # Fail closed for dynamic targets, parent traversal, shell expansion,
        # option flags, and absolute paths outside the mounted workspace.
        if len(argv) != 2:
            return _approval("moderate", "Directory change has no single explicit target.", matched=segment)
        target = argv[1]
        if (
            not target or target.startswith(("-", "~"))
            or _contains_variable_expansion(segment) or dynamic
            or any(char in target for char in "*?[]{}!\\`")
            or ".." in target.split("/")
            or any(ord(char) < 32 for char in target)
        ):
            return _approval("moderate", "Dynamic or escaping directory change.", matched=segment)
        if target.startswith("/") and not (
            target == "/workspace" or target.startswith("/workspace/")
        ):
            return _approval("moderate", "Absolute directory change outside the workspace mount.", matched=segment)
        if sensitive:
            return _approval("high", f"Directory change targets a sensitive path ({sensitive}).", matched=segment)
        return _low("Literal directory navigation within the workspace.", matched=segment)
    if executable == "xargs" and len(argv) > 1:
        return _approval(
            "moderate",
            "xargs delegates arguments into another process; requires approval.",
            matched=segment,
        )

    # Remote execution / download-and-execute.
    if executable in {"curl", "wget"} and dynamic:
        return _approval(
            "critical", "Download-and-execute pattern (command substitution in a network fetch).",
            matched=segment,
        )
    if executable in {"eval", "exec", "source", "."}:
        return _approval(
            "high", "Dynamic code evaluation/execution requires approval.",
            matched=segment,
        )
    if executable in {"python", "python3", "node", "deno", "ruby", "perl"}:
        joined = " ".join(argv[1:])
        if any(flag in argv for flag in ("-c", "--eval", "-e")) or "<<" in segment or "<<<" in segment:
            return _approval(
                "moderate", "Interpreter executing inline/dynamic code requires approval.",
                matched=segment,
            )
        if any(arg.endswith(".py") or arg.endswith(".js") or arg.endswith(".sh") for arg in argv[1:]):
            return _approval(
                "moderate", "Interpreter running an uninspected script requires approval.",
                matched=segment,
            )
        if executable in {"python", "python3"} and "-m" not in argv:
            return _approval(
                "moderate", "Interpreter invocation requires approval (dynamic effects).",
                matched=segment,
            )
        return _approval("moderate", "Interpreter invocation requires approval.", matched=segment)

    # Filesystem destruction.
    if executable == "rm":
        recursive = any(flag in {"-r", "-R", "-rf", "-fr", "--recursive", "-rfi", "-rif"} for flag in argv)
        if _targets_root_or_wildcard(argv):
            return _approval(
                "critical", "Deletion targeting a root/wildcard/home path.",
                matched=segment, scope="filesystem root",
            )
        if recursive:
            return _approval("high", "Recursive deletion.", matched=segment)
        if sensitive:
            return _approval(
                "critical", f"Deletion touching a sensitive path ({sensitive}).",
                matched=segment, scope=sensitive,
            )
        return _approval("moderate", "File deletion.", matched=segment)

    if executable in {"shred", "wipefs", "srm"}:
        return _approval("high", "Destructive file-wipe operation.", matched=segment)
    if executable in {"mkfs", "mkfs.ext4", "mkfs.xfs", "fdisk", "parted", "sgdisk", "badblocks"}:
        return _approval("critical", "Filesystem/partition formatting.", matched=segment)
    if executable == "dd":
        return _approval(
            "critical", "Raw block/device write (dd).", matched=segment,
            scope="block device",
        )
    if executable in {"mount", "umount", "losetup"}:
        return _approval("high", "Filesystem mount changes.", matched=segment)
    if executable in {"shutdown", "reboot", "halt", "poweroff", "init"}:
        return _approval("critical", "System shutdown/reboot.", matched=segment)
    if executable in {"systemctl", "service"}:
        action = next((arg for arg in argv[1:] if not arg.startswith("-")), "")
        if action in {"stop", "disable", "mask", "kill", "isolate"}:
            return _approval("high", f"Critical service management (systemctl {action}).", matched=segment)
        if action in {"restart"}:
            return _approval("moderate", "Service restart.", matched=segment)
        return _low("Read-only service inspection.", matched=segment)

    if executable in {"chmod", "chown", "chgrp", "umask"}:
        recursive = any(flag in {"-R", "-r", "--recursive"} for flag in argv)
        if sensitive:
            return _approval(
                "critical", f"Permission change on a sensitive path ({sensitive}).",
                matched=segment, scope=sensitive,
            )
        if recursive:
            return _approval("high", "Recursive permission/ownership change.", matched=segment)
        return _approval("moderate", "Permission/ownership change.", matched=segment)

    if executable == "mv" and (_targets_root_or_wildcard(argv) or dynamic):
        return _approval("moderate", "Move with wildcard/dynamic targets.", matched=segment)

    # Repository and development operations.
    if executable == "git":
        sub = " ".join(argv[1:3])
        if any(flag in argv for flag in ("--force", "-f")) and "push" in argv:
            return _approval("high", "Forced push rewrites remote history.", matched=segment)
        if "reset" in argv and "--hard" in argv:
            return _approval("high", "Destructive git reset discards changes.", matched=segment)
        if "clean" in argv and any(flag in {"-f", "-d", "-x", "-X", "-fd", "-df"} for flag in argv):
            return _approval("high", "git clean force-deletes untracked files.", matched=segment)
        if "push" in argv and any(token.startswith("+") for token in argv):
            return _approval("high", "Force-push refspec rewrites remote history.", matched=segment)
        if "filter-branch" in argv or ("rebase" in argv and any(flag in {"--hard", "--root"} for flag in argv)):
            return _approval("high", "History rewrite.", matched=segment)
        if sub.startswith("push") or sub.startswith("reset") or sub.startswith("clean"):
            return _approval("moderate", "Potentially destructive git operation.", matched=segment)
        if "rm" in argv:
            return _approval("moderate", "git rm removes tracked files.", matched=segment)
        if len(argv) > 1 and argv[1] in _GIT_SAFE_SUBCOMMANDS:
            return _low("Read-only git inspection.", matched=segment)
        return _approval("moderate", "Git operation may modify repository state.", matched=segment)

    if executable == "docker":
        if any(sub in argv[1:3] for sub in ("rm", "rmi")):
            return _approval("high", "Docker container/image removal.", matched=segment)
        if any(sub in argv[1:3] for sub in ("system", "volume", "container", "image", "network")) and any(
            sub in argv for sub in ("prune", "rm", "rmi")
        ):
            return _approval("high", "Docker prune/removal.", matched=segment)
        if "down" in argv and "-v" in argv:
            return _approval("high", "Compose down with volume destruction.", matched=segment)
        if "stop" in argv or "kill" in argv:
            return _approval("moderate", "Docker container stop/kill.", matched=segment)
        if len(argv) > 1 and (
            argv[1] in _DOCKER_SAFE_SUBCOMMANDS
            or (argv[1] == "compose" and len(argv) > 2 and argv[2] in {"ps", "logs", "config"})
        ):
            return _low("Read-only Docker inspection.", matched=segment)
        return _approval("moderate", "Docker operation may change runtime state.", matched=segment)

    if executable in {"dropdb", "mysqladmin"} or (
        executable == "psql" and any("-c" in arg for arg in argv)
    ) or (
        executable == "mysql" and any("-e" in arg for arg in argv)
    ):
        upper = segment.upper()
        if "DROP" in upper or "TRUNCATE" in upper:
            return _approval("critical", "Database destruction.", matched=segment)
        return _approval("moderate", "Direct database administration command.", matched=segment)

    if executable == "sed" and any(flag in {"-i", "--in-place"} for flag in argv):
        if sensitive:
            return _approval(
                "critical", f"In-place edit of a sensitive path ({sensitive}).",
                matched=segment, scope=sensitive,
            )
        return _approval("moderate", "In-place file edit (sed -i).", matched=segment)
    if executable == "find":
        if any(flag in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for flag in argv):
            return _approval(
                "moderate", "find with delete/exec actions.", matched=segment,
            )
        return _low("Read-only file search.", matched=segment)
    if executable == "awk" and "system" in segment:
        return _approval("moderate", "awk delegating to the shell (system()).", matched=segment)

    if sensitive:
        # Reading OR mutating a sensitive path is itself the risk (credential
        # access/exfiltration), including for otherwise read-only executables.
        return _approval(
            "high", f"Operation touching a sensitive path ({sensitive}).",
            matched=segment, scope=sensitive,
        )

    if executable in {"touch", "mkdir", "ln", "tee", "curl", "wget", "env", "printenv"}:
        return _approval(
            "moderate", "Command may mutate files, access sensitive environment data, "
            "or perform network effects.", matched=segment,
        )
    if executable in _SAFE_READ_ONLY or executable in {"pip", "npm", "yarn", "pnpm", "uv"}:
        # Package managers: list/show are read-only; installs mutate the
        # environment but are ordinary, reviewable development operations.
        if executable in {"pip", "npm", "yarn", "pnpm", "uv"}:
            mutating = any(sub in argv[1:3] for sub in ("install", "uninstall", "remove", "add", "publish"))
            if mutating:
                return _approval("moderate", "Package installation/removal.", matched=segment)
        return _low("Recognized low-risk read-only operation.", matched=segment)

    # Unknown executable: conservative by design.
    return _approval(
        "moderate",
        f"Command '{executable}' is not in the analyzed set; effects require review.",
        matched=segment, complete=True,
    )


def _substitution_payloads(segment: str) -> list[str]:
    """The inner texts of command/process substitutions in one stage."""
    import re

    payloads = []
    stripped = _strip_single_quotes(segment)
    for match in re.finditer(r"\$\(([^()]*)\)", stripped):
        payloads.append(match.group(1))
    for match in re.finditer(r"<\(([^()]*)\)", stripped):
        payloads.append(match.group(1))
    payloads.extend(payload[1:-1] for payload in re.findall(r"`[^`]+`", stripped))
    return payloads


def _strip_single_quotes(segment: str) -> str:
    out: list[str] = []
    quote = False
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if char == "'":
                quote = False
            index += 1
            continue
        if char == "'":
            quote = True
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _classify_stage(stage_text: str) -> CommandRiskClassification:
    """Classify one pipeline stage: redirect, substitution, then argv rules."""
    redirect = _redirect_target(stage_text)
    if redirect is not None:
        target, op = redirect
        if _mentions_sensitive_path(target):
            return _approval(
                "critical",
                f"Redirection writes to a sensitive path ({target}).",
                matched=stage_text[:200], scope=target,
            )
        return _approval(
            "moderate",
            f"File {('append' if op == '>>' else 'write')} via redirection.",
            matched=stage_text[:200], scope=target,
        )
    substitution = None
    if _contains_substitution(stage_text):
        substitution_payload = _substitution_payloads(stage_text)
        fetch_inside = any(
            (argv := _argv_of(payload)) is not None
            and _basename(argv[0]) in {"curl", "wget"}
            for payload in substitution_payload
        )
        substitution = _approval(
            "critical" if fetch_inside else "moderate",
            (
                "Download-and-execute via substitution (a network fetch runs inside the command)."
                if fetch_inside else "Command substitution embeds dynamic execution."
            ),
            matched=stage_text[:200],
        )
    argv = _argv_of(stage_text)
    if argv is None:
        return _approval(
            "high",
            "Command arguments could not be lexed reliably.",
            matched=stage_text[:200], complete=False,
        )
    result = _classify_simple_command(argv, stage_text)
    if substitution is not None:
        # The substitution executes before the command; the STRONGER verdict
        # of the two wins.
        return result if _rank(result) >= _rank(substitution) else substitution
    return result


def _classify_pipeline(stages: list[str]) -> CommandRiskClassification:
    """Classify a multi-stage pipeline as one data-flow expression.

    Pipelines connect stages: a network fetch feeding an interpreter is the
    download-and-execute pattern regardless of how harmless each stage looks
    alone. Every stage is still classified individually and the strongest
    verdict wins.
    """
    stage_texts = [stage.strip() for stage in stages if stage.strip()]
    if not stage_texts:
        return _low("Recognized low-risk operation.", "")
    worst: Optional[CommandRiskClassification] = None
    fetch_positions = []
    interpreter_positions = []
    for position, stage_text in enumerate(stage_texts):
        classification = _classify_stage(stage_text)
        if worst is None or _rank(classification) > _rank(worst):
            worst = classification
        argv = _argv_of(stage_text)
        executable = _basename(argv[0]) if argv else ""
        if executable in {"curl", "wget", "ftp", "nc", "ncat"}:
            fetch_positions.append(position)
        if executable in {"bash", "sh", "zsh", "dash", "python", "python3",
                          "node", "ruby", "perl", "eval"}:
            interpreter_positions.append(position)
    if worst is not None and worst.is_denied:
        return worst
    if fetch_positions and interpreter_positions and min(interpreter_positions) > min(fetch_positions):
        return _approval(
            "critical",
            "Download-and-execute via pipeline (a network fetch feeds an interpreter).",
            matched=" | ".join(stage_texts)[:200],
        )
    return worst or _low("Recognized low-risk operation.", "")


def classify_shell_command(command: str) -> CommandRiskClassification:
    """Classify one shell command line deterministically.

    Compound commands evaluate EVERY segment; one harmless subcommand never
    makes the whole expression safe. Quoted dangerous-looking text is not an
    executable operation; dynamic content is classified conservatively.
    """
    text = str(command or "").strip()
    if not text:
        return _low("Empty command.", "")
    backgrounded = False
    for marker in ("#!bg", "#bg", "# bg", "#background", "# background",
                   "@background", "# @background"):
        if text.lower().startswith(marker):
            text = text[len(marker):].lstrip(" :").strip()
            backgrounded = True
            break
    if not text:
        return _low("Empty command.", "")

    segments, complete = _split_top_level(text, ("&&", "||", ";", "\n", "&"))
    if not complete:
        return _approval(
            "high",
            "Command structure could not be analyzed reliably (unbalanced quoting).",
            matched=text[:200], complete=False,
        )

    worst: CommandRiskClassification = _low("Recognized low-risk operation.", "")
    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment or segment.startswith("#"):
            continue  # comments are not executable
        if _contains_heredoc(segment):
            classification = _approval(
                "moderate", "Here-document embeds dynamic script content.",
                matched=segment[:200], complete=True,
            )
        else:
            pipeline_stages, stages_complete = _split_top_level(segment, ("|",))
            if not stages_complete:
                classification = _approval(
                    "high", "Pipeline structure could not be analyzed reliably.",
                    matched=segment[:200], complete=False,
                )
            elif len(pipeline_stages) > 1:
                classification = _classify_pipeline(pipeline_stages)
            else:
                classification = _classify_stage(segment)
        if _rank(classification) > _rank(worst):
            worst = classification
        if worst.is_denied:
            break
    if backgrounded and worst.is_low_risk:
        worst = CommandRiskClassification(
            LOW_RISK, "low", "Low-risk command launched as a background job.",
            analysis_complete=True,
        )
    return worst


def _rank(classification: CommandRiskClassification) -> int:
    order = {"informational": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}
    return order.get(classification.level, 2)


def _contains_heredoc(segment: str) -> bool:
    stripped = _strip_quoted_parts(segment)
    return "<<" in stripped


def _redirect_target(segment: str) -> Optional[tuple[str, str]]:
    """The redirection target of a stage, when it writes to a file."""
    stripped = _strip_quoted_parts(segment)
    for op in (">>", ">", "&>", "&>>"):
        position = stripped.rfind(op)
        if position >= 0:
            target = stripped[position + len(op):].strip().split()[0] if (
                stripped[position + len(op):].strip()
            ) else ""
            if target and not target.startswith(("&", "/dev/null")):
                return target, op
    return None


def classify_python_code(code: str) -> CommandRiskClassification:
    """Conservative AST-based classification for the contained python tool."""
    import ast

    text = str(code or "")
    if not text.strip():
        return _low("Empty code.", "")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _approval(
            "moderate", "Code could not be analyzed (syntax error); effects unknown.",
            complete=False,
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            function = node.func
            if isinstance(function, ast.Name):
                name = function.id
            elif isinstance(function, ast.Attribute):
                name = function.attr
            if name in {"system", "popen", "rmtree", "unlink", "remove", "rename",
                        "eval", "exec", "compile", "__import__", "runcall",
                        "run", "call", "check_call", "check_output"}:
                return _approval(
                    "moderate", f"Python code calls {name}(); requires approval.",
                    matched=name,
                )
            if name == "open":
                mode_value = ""
                args = node.args or ()
                if len(args) >= 2 and isinstance(args[1], ast.Constant):
                    mode_value = str(args[1].value or "")
                for keyword in node.keywords or ():
                    if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                        mode_value = str(keyword.value.value or "")
                if any(flag in mode_value for flag in ("w", "a", "x", "+")):
                    return _approval(
                        "moderate", "Python code opens a file for writing.",
                        matched="open(...)",
                    )
        if isinstance(node, ast.Attribute) and node.attr in {
            "system", "popen", "rmtree", "unlink",
        }:
            return _approval(
                "moderate", f"Python code references {node.attr}; requires approval.",
                matched=node.attr,
            )
    return _low("Contained python code with no mutating calls detected.")
