"""M5.3 increment 1: the single session working-directory contract.

One authoritative, owner-scoped, session-scoped CWD per Gateway session,
persisted on the shared session row (``sessions.cwd``) and honored by file
tools, shell/process execution, agent context and approval bindings.

Precedence (documented, deterministic):

1. An explicit request value the caller is authorized to bind (per turn).
2. The session's persisted ``cwd``.
3. The client-specific default captured at session creation (CLI/TUI: the
   invoking shell's ``pwd``; Web/Telegram/remote-default: the configured
   execution account's HOME).
4. No binding at all — the global tool-path allowlist applies (never a
   silent relocation and never the filesystem root).

CWD is NOT the security sandbox: binding a directory selects where relative
paths resolve and subprocesses run; every access still passes the existing
authority, ``vet_workspace`` sensitivity checks, in-workspace deny lists and
resource confinement. Selecting a directory never grants access to unrelated
paths. A selected CWD must never be the filesystem root, and the model cannot
change the authoritative CWD with generated text — only an authorized client
call through the session API (validated server-side) or an explicitly posted,
per-turn bind can.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "execution_home",
    "default_session_cwd_for_surface",
    "vetted_session_cwd",
    "clearable_session_cwd",
]

# Client surfaces that run on the same host as the execution account and may
# supply the invoking shell's working directory. Web browsers and future
# remote/Telegram clients cannot — their default is the execution HOME.
_LOCAL_HOST_SURFACES = frozenset({"odysseus-tui", "cli"})


def execution_home() -> Optional[str]:
    """The configured execution account's HOME, validated as a real directory.

    Falls back to ``None`` when HOME is unset or not a real directory — the
    caller then keeps no binding (the global allowlist applies) instead of
    silently binding something else.
    """
    home = os.path.expanduser("~")
    if not home or home == "~":
        return None
    resolved = os.path.realpath(home)
    if not os.path.isdir(resolved):
        return None
    return resolved


def vetted_session_cwd(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate a requested session CWD server-side: (canonical, reason).

    The reason is an actionable client-facing message for every refusal:
    nonexistent, not a directory, a sensitive path, a deny-listed path, or a
    filesystem root. Reuses the dispatcher's ``vet_workspace`` checks so a
    directory that could not serve as a bound workspace is never stored as a
    session CWD either.
    """
    requested = str(raw or "").strip()
    if not requested:
        return None, "Directory is empty."
    if any(ord(char) < 32 for char in requested) or len(requested) > 4096:
        return None, "Directory contains control characters or is too long."
    resolved = os.path.realpath(os.path.expanduser(requested))
    if not os.path.isdir(resolved):
        return None, f"Directory does not exist or is not accessible: {requested}"
    from src.tool_execution import vet_workspace

    canonical = vet_workspace(resolved)
    if canonical is None:
        if os.path.dirname(resolved) == resolved:
            return None, (
                f"Refusing to select the filesystem root ({requested}) as a "
                "session working directory."
            )
        return None, (
            f"Directory is not selectable as a working directory "
            f"(sensitive or restricted): {requested}"
        )
    return canonical, None


def default_session_cwd_for_surface(
    surface: Optional[str],
    *,
    invoking_cwd: Optional[str] = None,
    remote: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """The natural default CWD for a new session on this client surface.

    Returns ``(canonical, reason)``. Local CLI/TUI sessions start in the
    invoking shell's actual working directory (validated; an unusable ``pwd``
    falls back to the execution HOME rather than silently substituting a
    repository root or a generated conversation workspace). Web, Telegram and
    remote sessions default to the execution account's HOME — the browser
    never has server filesystem access, and a remote client's local ``pwd``
    does not exist on the execution host. Filesystem root is never selected.
    """
    normalized_surface = str(surface or "").strip().lower()
    if not remote and normalized_surface in _LOCAL_HOST_SURFACES and invoking_cwd:
        canonical, reason = vetted_session_cwd(invoking_cwd)
        if canonical is not None:
            return canonical, None
        logger.info(
            "session-cwd: invoking pwd unusable (%s); falling back to execution HOME",
            reason,
        )
    home = execution_home()
    if home is None:
        return None, "Execution account HOME is not available for a default working directory."
    canonical, reason = vetted_session_cwd(home)
    if canonical is None:
        return None, reason or "Execution HOME is not selectable."
    return canonical, None


def clearable_session_cwd(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Normalize a stored session CWD value: ``None``/empty clears it."""
    requested = str(raw or "").strip()
    if not requested:
        return None, None
    return vetted_session_cwd(requested)
