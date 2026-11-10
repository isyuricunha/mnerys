"""Shared publication boundary for derived-artifact writers (M5.2 increment 4).

Derived-artifact writers — ``inspect_media`` still/clip/SVG exports, the
``transcribe_media`` transcript destination, and future media/document
generators — publish generated outputs to workspace destinations through this
module instead of writing destination paths directly (ffmpeg ``-y``,
``Path.write_text``). Generator subprocesses never touch the final path; they
write a staged file beside the destination, and publication is the single
authorized step that can place content at the destination.

Two destination classes are distinguished explicitly:

``user_file``
    A user-visible workspace file. Publishing over an EXISTING file requires
    a real, server-recorded read baseline (same owner and session, identity +
    content digest still matching) exactly like the registry's text-mutation
    tools — enforced through the same :mod:`src.agent_runtime.read_baselines`
    store, so code-execution invalidation, expiry, and eviction apply here
    too. Creating a NEW artifact needs no prior read but is exclusive: a file
    that appears at the destination is never overwritten (the caller is told
    to Read it first or choose another name).

``internal_cache``
    A disposable implementation artifact under a server-derived name (for
    example the automatic transcript cache under ``.odysseus/transcripts/``).
    No read baseline is required — no user-owned file is being mutated — but
    publication is still atomic and refuses to write through a symlink.

Publication mechanics (the same honesty contract as the increment-2 text
tools):

* The destination's parent directory is opened with ``O_NOFOLLOW`` on the
  final component and the destination itself is always addressed through that
  directory fd with ``O_NOFOLLOW``, so a symlink at the destination is
  refused, never followed or written through.
* An existing destination is validated (baseline + identity + digest) on a
  descriptor holding an exclusive ``flock``, and immediately before the
  atomic ``os.replace`` publication the path is re-opened and must still
  resolve to the exact validated inode. Cooperating writers of this server
  are therefore serialized and a replaced destination is detected instead of
  clobbered. A non-cooperating writer (a running shell command) between that
  re-open and the rename remains the documented residual race, the same class
  the text tools document.
* A successfully published user file is re-baselined from the exact bytes the
  server just published (server-observed), so create→replace and export→re-export
  flows work without a forced reread, exactly like every other successful
  mutation.

Files above :data:`src.agent_runtime.read_baselines.DEFAULT_MAX_BASELINE_FILE_BYTES`
have no baseline (fail-closed): replacing such an existing destination is
refused with guidance to choose a new name.
"""
from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.agent_runtime.read_baselines import (
    OBSERVATION_FULL,
    ReadBaselineError,
    record_file_read,
    require_baseline_for_derived_mutation,
)

__all__ = [
    "ArtifactPublicationError",
    "DestinationAppeared",
    "DestinationReplacedSinceValidation",
    "INTERNAL_CACHE",
    "USER_FILE",
    "UnsafeDestination",
    "create_exclusive_file",
    "ensure_destination_publishable",
    "is_read_baseline_refusal",
    "publish_staged_artifact",
    "staged_artifact_path",
]

USER_FILE = "user_file"
INTERNAL_CACHE = "internal_cache"

_STAGED_PREFIX = ".odysseus-publish-"


class ArtifactPublicationError(ValueError):
    """A derived artifact could not be published to its destination."""


class UnsafeDestination(ArtifactPublicationError):
    """The destination is a symlink, a directory, or otherwise not a plain
    file this boundary may publish."""


class DestinationAppeared(ArtifactPublicationError):
    """A file appeared at the destination while the artifact was being
    generated; exclusive publication did not overwrite it."""


class DestinationReplacedSinceValidation(ArtifactPublicationError):
    """The destination path came to resolve to a different file object
    between baseline validation and publication; the publish was aborted."""


@contextmanager
def staged_artifact_path(destination: Path) -> Iterator[Path]:
    """Yield an exclusive staged file path beside ``destination``.

    The staged file lives in the destination's directory (same filesystem, so
    publication is a rename/link), keeps the destination's suffix (generators
    pick their muxer/encoder from it), and is created with the ordinary
    ``0o666 & ~umask`` permissions so a published artifact is readable by the
    server like any other tool-written file. The staged file is removed on
    context exit unless publication already consumed its directory entry.
    """
    directory = destination.parent
    if not directory.is_dir():
        raise UnsafeDestination(
            f"destination directory does not exist: {directory}"
        )
    staged = None
    for _ in range(8):
        candidate = directory / (
            f"{_STAGED_PREFIX}{secrets.token_hex(16)}{destination.suffix}"
        )
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        staged = candidate
        break
    if staged is None:
        raise ArtifactPublicationError(
            "could not create a staged artifact file next to the destination"
        )
    try:
        yield staged
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _open_directory_fd(destination: Path) -> int:
    return os.open(
        os.path.dirname(destination) or ".",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


def _lstat_destination(dir_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _refuse_unsafe_existing(info: os.stat_result, destination: Path) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeDestination(
            f"{destination} is a symlink; derived artifacts are never "
            "published through or over symlinks. Choose a plain destination."
        )
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeDestination(
            f"{destination} exists and is not a regular file; choose a "
            "different artifact destination."
        )


@contextmanager
def _locked_destination_fd(dir_fd: int, name: str):
    """Open the existing destination through the parent directory fd with
    ``O_NOFOLLOW`` and hold an exclusive ``flock`` on that descriptor for the
    whole validate-and-publish sequence. The descriptor pins the inode: a
    path swap after the open cannot redirect the validation, and cooperating
    writers of this server are mutually excluded until the lock is released."""
    import fcntl

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_fd_bytes(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_existing_for_user_file(
    owner,
    session_id,
    destination: Path,
    *,
    tool: str,
    fd: int,
) -> None:
    """Validate an existing destination on the descriptor it will be replaced
    through: the exclusive flock is already held by the caller, bytes are read
    from the descriptor, and the baseline is checked against the descriptor's
    identity and content. Raises the actionable :class:`ReadBaselineError`
    subclasses on every refusal."""
    info = os.fstat(fd)
    from src.agent_runtime.resources import FileObjectIdentity

    require_baseline_for_derived_mutation(
        owner,
        session_id,
        str(destination),
        tool=tool,
        current_bytes=_read_fd_bytes(fd),
        current_identity=FileObjectIdentity(info.st_dev, info.st_ino, "file"),
    )


def ensure_destination_publishable(
    owner,
    session_id,
    destination: Path,
    *,
    tool: str,
    classification: str = USER_FILE,
) -> None:
    """Fast, advisory pre-check before an expensive generator runs.

    Applies the same refusals as publication (symlink, non-regular file,
    missing baseline) so a derived writer can fail with actionable feedback
    before generating anything. The authoritative check happens again at
    publication time; this probe never creates or mutates anything.
    """
    dir_fd = _open_directory_fd(destination)
    try:
        info = _lstat_destination(dir_fd, destination.name)
        if info is None:
            return
        _refuse_unsafe_existing(info, destination)
        if classification == INTERNAL_CACHE:
            return
        with _locked_destination_fd(dir_fd, destination.name) as fd:
            _validate_existing_for_user_file(
                owner, session_id, destination, tool=tool, fd=fd
            )
    finally:
        os.close(dir_fd)


def publish_staged_artifact(
    owner,
    session_id,
    destination: Path,
    staged: Path,
    *,
    tool: str,
    classification: str = USER_FILE,
) -> str:
    """Publish a staged artifact to its destination.

    Returns ``"created"`` when the destination did not exist and was claimed
    exclusively, ``"replaced"`` when an existing file was replaced after its
    baseline was validated. Raises :class:`ArtifactPublicationError` (or the
    actionable :class:`ReadBaselineError` subclasses) before any byte of the
    destination changes. On success the staged directory entry is consumed.
    """
    dir_fd = _open_directory_fd(destination)
    try:
        name = destination.name
        info = _lstat_destination(dir_fd, name)
        if info is not None:
            _refuse_unsafe_existing(info, destination)
            if classification == INTERNAL_CACHE:
                # Disposable cache under a server-derived name: atomic rename
                # publication, no baseline, but never through a symlink.
                os.replace(
                    staged.name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                )
                return "replaced"
            with _locked_destination_fd(dir_fd, name) as fd:
                _validate_existing_for_user_file(
                    owner, session_id, destination, tool=tool, fd=fd
                )
                # Still under the held lock: copy the existing file's
                # permissions onto the staged file, re-open the path and
                # require it to still resolve to the validated inode, then
                # atomically replace it.
                os.chmod(staged, stat.S_IMODE(os.fstat(fd).st_mode))
                reopen = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    current = os.fstat(reopen)
                    pinned = os.fstat(fd)
                    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                        raise DestinationReplacedSinceValidation(
                            f"{destination} was replaced while the artifact was "
                            "being published; nothing was overwritten. Read it "
                            f"again, then retry {tool}."
                        )
                finally:
                    os.close(reopen)
                os.replace(
                    staged.name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                )
            if classification == USER_FILE:
                # The server observed the exact bytes it just published:
                # re-baseline so a follow-up mutation of this file validates
                # against them (fail-closed when unobservable).
                record_file_read(
                    owner, session_id, str(destination), (OBSERVATION_FULL, 1, 0)
                )
            return "replaced"
        # New artifact: exclusive publication. os.link fails when the
        # destination appeared (including as a symlink) — nothing is ever
        # overwritten on the create path.
        try:
            os.link(
                staged.name,
                name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            appeared = _lstat_destination(dir_fd, name)
            if appeared is not None and stat.S_ISLNK(appeared.st_mode):
                raise UnsafeDestination(
                    f"{destination} appeared as a symlink while the artifact "
                    "was being generated; nothing was written through it."
                ) from None
            raise DestinationAppeared(
                f"{destination} appeared while the artifact was being generated "
                "and was not overwritten. Read it first if you intend to "
                f"replace it, then retry {tool}, or choose a new destination."
            ) from None
        try:
            os.unlink(staged.name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        if classification == USER_FILE:
            record_file_read(
                owner, session_id, str(destination), (OBSERVATION_FULL, 1, 0)
            )
        return "created"
    finally:
        os.close(dir_fd)


def create_exclusive_file(path: Path, data: bytes) -> None:
    """Create a new internal artifact file exclusively.

    For server-managed directories with generated names (the image gallery):
    a name collision never silently overwrites whatever is already there.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def is_read_baseline_refusal(error: Exception) -> bool:
    """Whether the error is a read-before-edit refusal from the shared store."""
    return isinstance(error, ReadBaselineError)
