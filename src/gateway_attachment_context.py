"""Owner-bound, read-only text-upload snapshot for direct admitted turns.

This opt-in profile reuses the Web upload index/manifest and ChatHandler
document preprocessing. It is not a general upload API, a filesystem grant,
or a safe multimodal transport. File contents remain untrusted model context.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any

from src.gateway_admission import AdmissionDenied

_MAX_ATTACHMENTS = 4
_MAX_BYTES_PER_FILE = 64 * 1024
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".log"})
_TEXT_MIMES = frozenset({
    "text/plain", "text/markdown", "text/csv",
    "application/json", "application/octet-stream",
})
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_SHA_PATTERN = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class AdmittedTextAttachment:
    attachment_id: str
    name: str
    mime: str
    path: str
    size: int
    checksum_sha256: str
    device: int
    inode: int


def validate_text_attachment_ids(attachment_ids: Any) -> tuple[str, ...]:
    if (
        type(attachment_ids) is not tuple
        or len(attachment_ids) > _MAX_ATTACHMENTS
        or any(
            type(item) is not str
            or not _ID_PATTERN.fullmatch(item)
            for item in attachment_ids
        )
        or len(set(attachment_ids)) != len(attachment_ids)
    ):
        raise AdmissionDenied(403, "Invalid direct attachment selection")
    return attachment_ids


def resolve_admitted_text_attachments(
    *, attachment_ids: tuple[str, ...], owner: str | None,
    upload_handler: Any, ports: Any,
) -> tuple[AdmittedTextAttachment, ...]:
    """Resolve and check unchanged owner-scoped Web uploads before use."""
    if not attachment_ids:
        return ()
    if (
        upload_handler is None
        or not callable(getattr(upload_handler, "resolve_upload", None))
    ):
        raise AdmissionDenied(403, "Trusted upload resolver is required")
    try:
        manifest = ports.upload_manifest(list(attachment_ids), upload_handler, owner)
        if (
            not isinstance(manifest, list)
            or len(manifest) != len(attachment_ids)
        ):
            raise ValueError("Missing owner-scoped upload manifest")

        resolved = []
        for attachment_id, entry in zip(attachment_ids, manifest):
            if (
                not isinstance(entry, dict)
                or entry.get("id") != attachment_id
                or entry.get("read_policy") != "owner_checked_upload"
            ):
                raise ValueError("Invalid upload manifest identity")
            info = upload_handler.resolve_upload(attachment_id, owner=owner)
            if (
                not isinstance(info, dict)
                or info.get("id") != attachment_id
                or info.get("owner") != owner
            ):
                raise ValueError("Upload owner no longer matches admission")
            path = info.get("path")
            if (
                type(path) is not str or not os.path.isabs(path)
                or os.path.realpath(path) != path
                or entry.get("path") != path
                or os.path.islink(path)
            ):
                raise ValueError("Upload path is not canonically confined")
            inside = getattr(upload_handler, "_inside_upload_dir", None)
            if inside is None:
                inside = getattr(upload_handler, "inside_base_dir", None)
            if not callable(inside) or not inside(path):
                raise ValueError("Upload path is outside its configured root")

            name = info.get("name") or info.get("original_name")
            mime = info.get("mime")
            if (
                type(name) is not str or not name or len(name) > 255
                or Path(name).suffix.lower() not in _TEXT_EXTENSIONS
                or type(mime) is not str or mime not in _TEXT_MIMES
                or entry.get("name") != name
                or entry.get("mime") != mime
            ):
                raise ValueError("Unsupported direct text attachment")
            size = info.get("size")
            checksum = info.get("checksum_sha256") or info.get("hash")
            if (
                type(size) is not int or not 0 < size <= _MAX_BYTES_PER_FILE
                or type(checksum) is not str
                or not _SHA_PATTERN.fullmatch(checksum)
                or entry.get("size") != size
                or entry.get("checksum_sha256") != checksum
            ):
                raise ValueError("Invalid uploaded text snapshot")
            # Small bounded read; reject index swaps and in-place modifications.
            # A second lookup after async preprocessing checks this identity.
            with open(path, "rb") as handle:
                file_stat = os.fstat(handle.fileno())
                if (
                    not os.path.isfile(path)
                    or file_stat.st_size != size
                    or (file_stat.st_dev, file_stat.st_ino)
                    != (os.stat(path).st_dev, os.stat(path).st_ino)
                ):
                    raise ValueError("Upload identity changed")
                payload = handle.read(_MAX_BYTES_PER_FILE + 1)
            if len(payload) != size or hashlib.sha256(payload).hexdigest() != checksum:
                raise ValueError("Upload content changed")
            if b"\x00" in payload:
                raise ValueError("Binary content is not supported by this text profile")
            payload.decode("utf-8")
            resolved.append(AdmittedTextAttachment(
                attachment_id=attachment_id, name=name, mime=mime, path=path,
                size=size, checksum_sha256=checksum,
                device=file_stat.st_dev, inode=file_stat.st_ino,
            ))
        return tuple(resolved)
    except AdmissionDenied:
        raise
    except Exception as exc:
        # Never reflect filenames, raw content, local paths or index secrets.
        raise AdmissionDenied(409, "Selected text attachment is unavailable") from exc
