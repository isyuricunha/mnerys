"""Server-owned read baselines for read-before-edit protection (M5.2 increment 2).

The registry's file-mutation tools — ``write_file``, ``edit_file`` and
``apply_patch`` — may mutate an EXISTING file only when the same owner and
session recorded a real, successful server-side read of that exact file, and
the file still matches what was observed. The baseline is pure server state:
the model never sees or supplies a version token, so there is nothing to forge.

What a baseline binds
    (owner, session) → resolved path → the observed file identity (device,
    inode via :class:`FileObjectIdentity`), the full-content SHA-256 digest,
    size, mtime_ns, and how much of the file the read actually observed:

    * ``FULL``       — the whole file was observed; authorizes in-place
      overwrite (``write_file``) and any anchored edit (``edit_file`` /
      ``apply_patch``).
    * ``LINES``      — a line-range read (offset/limit, or a truncated read
      that stopped mid-file). Authorizes an anchored edit only when every
      occurrence of the anchor text lies inside the observed span; never
      authorizes replacing unread content with a full-file overwrite.
    * ``OPAQUE``     — the observation is derived content (structured document
      extraction), not the raw file text; records the observation but
      authorizes no mutation (such files are not text-editable anyway).

When a baseline stops being valid
    * TTL expiry (read too old to reason about — the agent rereads);
    * store eviction (bounded memory; oldest baselines fall off first);
    * file replacement (identity changed: the path now resolves to a
      different inode — delete-and-recreate, rename-swap, or symlink swap);
    * content change (same inode, different bytes — digest mismatch);
    * code execution (``bash`` / ``python`` runs can mutate any file without
      a registry tool call, so every baseline of the owner/session is
      invalidated after one — a later edit must reread; this is the honest
      boundary of the guarantee, see the architecture note);
    * process restart (the store is in-memory, matching the approval store:
      a restart is a new incarnation and clears all baselines);
    * resolution/permission changes (a mutation path that can no longer
      resolve the path through the active confinement never reaches the
      baseline check at all).

Validation-to-mutation sequence
    The mutating tools open the target through a parent-directory fd with
    ``O_NOFOLLOW`` on the final component, take an exclusive ``flock`` on that
    fd, and validate identity + content digest on the SAME descriptor they
    later rewrite in place. A path swap after the open therefore cannot
    redirect the write, and every cooperating writer of this server (all
    processes) serializes on the same per-inode lock. Two residual races are
    documented rather than hidden: a non-cooperating writer (a running shell
    command) can still touch the file between digest validation and write,
    and a crash mid-write can leave partial bytes — the same in-place
    semantics the previous writer had, now guarded by identity + digest.

Relationship to the security architecture
    Baseline enforcement sits INSIDE tool execution, after admission,
    authority, exact-operation and approval gates. It can only refuse — it
    never widens anything, and a missing/stale baseline produces an
    actionable error so the agent can reread and retry through a fresh
    authorized operation.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

from src.agent_runtime.resources import FileObjectIdentity

__all__ = [
    "AnchorOutsideObservedLines",
    "BaselineRequired",
    "FileModifiedSinceRead",
    "FileReplacedSinceRead",
    "Observation",
    "PartialReadInsufficient",
    "ReadBaseline",
    "ReadBaselineError",
    "ReadBaselineStore",
    "invalidate_for_code_execution",
    "observed_line_span_covers",
    "read_baseline_store",
    "record_file_read",
    "require_baseline_for_derived_mutation",
    "require_baseline_for_mutation",
]

DEFAULT_BASELINE_TTL_SECONDS = 3600
DEFAULT_MAX_BASELINES = 2048
# Files above this size get no baseline at all: digesting them would cost a
# full read per mutation check, and the text tools' 20k-char observation cap
# means such a file could never be fully observed anyway. Mutations to them
# are refused until a future increment defines a streaming contract.
DEFAULT_MAX_BASELINE_FILE_BYTES = 16 * 1024 * 1024

# (kind, start_line, end_line); start/end are 1-based and inclusive, and are
# meaningless for full/opaque observations.
Observation = Tuple[str, int, int]

OBSERVATION_FULL = "full"
OBSERVATION_LINES = "lines"
OBSERVATION_OPAQUE = "opaque"


class ReadBaselineError(ValueError):
    """A file mutation violated the read-before-edit contract."""


class BaselineRequired(ReadBaselineError):
    """No recorded read of this file exists for this owner and session."""


class FileReplacedSinceRead(ReadBaselineError):
    """The path resolves to a different file object than the one read."""


class FileModifiedSinceRead(ReadBaselineError):
    """The file's content changed since the recorded read."""


class PartialReadInsufficient(ReadBaselineError):
    """Only part of the file was observed; it cannot authorize this mutation."""


class AnchorOutsideObservedLines(ReadBaselineError):
    """The edit anchor lies outside the lines the read actually observed."""


@dataclass(frozen=True)
class ReadBaseline:
    owner: str
    session_id: str
    path: str
    identity: FileObjectIdentity
    content_sha256: str
    size: int
    mtime_ns: int
    observation: Observation
    recorded_at: float

    @property
    def full_read(self) -> bool:
        return self.observation[0] == OBSERVATION_FULL


class ReadBaselineStore:
    """Thread-safe in-memory registry of read baselines, mirroring the
    in-memory approval store's lifecycle (TTL, bounded size, process-local)."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_BASELINE_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_BASELINES,
    ):
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._baselines: Dict[Tuple[str, str, str], ReadBaseline] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(owner, session_id, path) -> Tuple[str, str, str]:
        return (
            str(owner or "").strip().casefold(),
            str(session_id or "").strip(),
            str(path or ""),
        )

    def record(self, baseline: ReadBaseline) -> None:
        key = self._key(baseline.owner, baseline.session_id, baseline.path)
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            self._baselines[key] = baseline
            self._evict_oldest_locked()

    def get(self, owner, session_id, path) -> Optional[ReadBaseline]:
        key = self._key(owner, session_id, path)
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            return self._baselines.get(key)

    def invalidate(self, owner, session_id, path=None) -> int:
        """Drop one path's baseline, or every baseline of the session."""
        prefix = self._key(owner, session_id, "")[:2]
        with self._lock:
            if path is not None:
                key = self._key(owner, session_id, path)
                return 1 if self._baselines.pop(key, None) is not None else 0
            doomed = [k for k in self._baselines if k[:2] == prefix]
            for key in doomed:
                self._baselines.pop(key, None)
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._baselines.clear()

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            key for key, baseline in self._baselines.items()
            if baseline.recorded_at + self._ttl_seconds <= now
        ]
        for key in expired:
            self._baselines.pop(key, None)

    def _evict_oldest_locked(self) -> None:
        while len(self._baselines) > self._max_entries:
            oldest = min(self._baselines.items(), key=lambda item: item[1].recorded_at)
            self._baselines.pop(oldest[0], None)


_store = ReadBaselineStore()
_store_lock = threading.Lock()


def read_baseline_store() -> ReadBaselineStore:
    return _store


def record_file_read(
    owner,
    session_id,
    path: str,
    observation: Observation,
) -> Optional[ReadBaseline]:
    """Record a real, successful read as the mutation baseline for this
    owner/session/path. Computes the file identity and full-content digest
    from the filesystem at recording time; a file that cannot be observed
    simply yields no baseline (fail-closed)."""
    try:
        info = os.stat(path)
        if info.st_size > DEFAULT_MAX_BASELINE_FILE_BYTES:
            return None
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
    kind, start, end = observation
    baseline = ReadBaseline(
        owner=str(owner or "").strip().casefold(),
        session_id=str(session_id or "").strip(),
        path=str(path),
        identity=FileObjectIdentity(info.st_dev, info.st_ino, "file"),
        content_sha256=digest,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        observation=(str(kind), int(start), int(end)),
        recorded_at=time.time(),
    )
    _store.record(baseline)
    return baseline


def _validated_baseline(
    owner,
    session_id,
    path: str,
    *,
    tool: str,
    current_bytes: Optional[bytes] = None,
    current_identity: Optional[FileObjectIdentity] = None,
) -> ReadBaseline:
    """Shared core of every baseline check: the recorded read must exist for
    this owner/session/path, and the file object being mutated (identity +
    content digest, observed on the descriptor the caller will write through)
    must still be the exact one that was read. Single enforcement point for
    the registry's text tools and the derived-artifact writers alike."""
    baseline = _store.get(owner, session_id, path)
    if baseline is None:
        raise BaselineRequired(
            f"No recorded read of {path} in this session. "
            f"Call Read on it first, then retry {tool}."
        )
    if current_identity is not None and current_identity != baseline.identity:
        raise FileReplacedSinceRead(
            f"{path} was replaced or recreated since it was read. "
            f"Read it again to observe the current file, then retry {tool}."
        )
    if current_bytes is not None:
        digest = hashlib.sha256(current_bytes).hexdigest()
        if digest != baseline.content_sha256:
            raise FileModifiedSinceRead(
                f"{path} changed since it was read (content digest mismatch). "
                f"Read it again to see the current content, then retry {tool}."
            )
    return baseline


def _observation_span_label(baseline: ReadBaseline) -> str:
    kind, start, end = baseline.observation
    if kind == OBSERVATION_LINES:
        return f"lines {start}-{end}"
    return "derived document text"


def require_baseline_for_mutation(
    owner,
    session_id,
    path: str,
    *,
    tool: str,
    current_bytes: Optional[bytes] = None,
    current_identity: Optional[FileObjectIdentity] = None,
) -> ReadBaseline:
    """Validate the read-before-edit contract for one mutating tool call.

    ``current_bytes`` / ``current_identity`` come from the descriptor the
    caller will mutate through, so the check and the mutation observe the
    same file object. Raises a :class:`ReadBaselineError` subclass with an
    actionable message on every violation.
    """
    baseline = _validated_baseline(
        owner, session_id, path,
        tool=tool,
        current_bytes=current_bytes,
        current_identity=current_identity,
    )
    if tool == "write_file" and not baseline.full_read:
        raise PartialReadInsufficient(
            f"{path} was only partially read ({_observation_span_label(baseline)}); "
            f"replacing unread content is not allowed. Read the whole file with "
            f"Read, then retry write_file."
        )
    if tool == "apply_patch" and not baseline.full_read:
        # A patch's context lines must match the whole current file; patching
        # from a partial observation would silently trust unread content.
        raise PartialReadInsufficient(
            f"{path} was only partially read ({_observation_span_label(baseline)}); "
            f"applying a patch to unread content is not allowed. Read the whole "
            f"file with Read, then retry apply_patch."
        )
    return baseline


def require_baseline_for_derived_mutation(
    owner,
    session_id,
    path: str,
    *,
    tool: str,
    current_bytes: Optional[bytes] = None,
    current_identity: Optional[FileObjectIdentity] = None,
) -> ReadBaseline:
    """Validate the read-before-edit contract for a DERIVED-artifact writer
    (``inspect_media`` exports, ``transcribe_media`` transcripts, …) that
    replaces an existing file with freshly generated content.

    The rule differs from the text tools by construction: the new content is
    not derived from the old bytes, so line-scope anchoring does not apply.
    What is still required is real awareness of the file being replaced — a
    successful server-recorded read (FULL or LINES) of this exact file by the
    same owner and session, with identity and content digest unchanged. An
    OPAQUE observation (structured document extraction) never authorizes a
    raw-byte replacement, and a file above the baseline size bound has no
    baseline at all, so such destinations are refused (choose a new name).
    """
    baseline = _validated_baseline(
        owner, session_id, path,
        tool=tool,
        current_bytes=current_bytes,
        current_identity=current_identity,
    )
    if baseline.observation[0] == OBSERVATION_OPAQUE:
        raise PartialReadInsufficient(
            f"{path} was only observed as derived document text, not as the "
            f"file itself; {tool} may not replace it. Read the file with Read, "
            f"then retry {tool}."
        )
    return baseline


def observed_line_span_covers(baseline: ReadBaseline, text: str, needle: str) -> bool:
    """Whether every occurrence of ``needle`` in ``text`` lies fully inside
    the line span a partial read observed. Full reads cover everything."""
    if baseline.full_read:
        return True
    kind, start, end = baseline.observation
    if kind != OBSERVATION_LINES:
        return False
    # Prefix of the text at the start of line `start` (1-based inclusive).
    lines = text.split("\n")
    line_offsets = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1
    if start < 1 or start > len(lines):
        return False
    span_start = line_offsets[start - 1]
    span_end = line_offsets[end] - 1 if end < len(lines) else len(text)
    position = text.find(needle)
    while position != -1:
        if position < span_start or position + len(needle) > span_end:
            return False
        position = text.find(needle, position + 1)
    return True


def invalidate_for_code_execution(owner, session_id) -> int:
    """Invalidate every baseline of this owner/session after code execution.

    ``bash`` and ``python`` can mutate any file without a registry tool call,
    so content the agent read earlier can no longer be trusted as observed.
    The next edit must reread — converting the shell bypass into a visible,
    recoverable contract instead of a silent stale-edit window.
    """
    return _store.invalidate(owner, session_id)
