"""Owner-bound upload snapshots for direct admitted turns.

This opt-in profile reuses the Web upload index/manifest and ChatHandler
preprocessing. It is not a general upload API, a filesystem grant, or a
safe multimodal transport. File contents remain untrusted model context.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any

from src.gateway_admission import AdmissionDenied
from src.upload_limits import DEFAULT_CHAT_UPLOAD_MAX_BYTES

_MAX_ATTACHMENTS = 4
_MAX_BYTES_PER_TEXT_FILE = 64 * 1024
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".log"})
_TEXT_MIMES = frozenset({
    "text/plain", "text/markdown", "text/csv",
    "application/json", "application/octet-stream",
})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_SHA_PATTERN = re.compile(r"[a-f0-9]{64}")

_TEXT_PROFILE = "text"
_IMAGE_PROFILE = "text_and_image"
_ATTACHMENT_PROFILES = frozenset({_TEXT_PROFILE, _IMAGE_PROFILE})


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
    kind: str = "text"


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


def _image_upload(
    name: str, mime: str, max_chat_upload_bytes: int,
) -> bool:
    """Classify one admitted entry as an owner-checked image upload."""
    return (
        Path(name).suffix.lower() in _IMAGE_EXTENSIONS
        and type(mime) is str and mime.startswith("image/")
        and 0 < max_chat_upload_bytes
    )


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_admitted_attachments(
    *,
    attachment_ids: tuple[str, ...],
    owner: str | None,
    upload_handler: Any,
    ports: Any,
    profile: str = _TEXT_PROFILE,
    max_chat_upload_bytes: int | None = None,
) -> tuple[AdmittedTextAttachment, ...]:
    """Resolve and check unchanged owner-scoped Web uploads before use.

    ``profile="text"`` keeps the original bounded UTF-8 text contract.
    ``profile="text_and_image"`` additionally admits owner-checked image
    uploads (vision turns); image bytes are never extracted as prompt text.
    """
    if profile not in _ATTACHMENT_PROFILES:
        raise AdmissionDenied(403, "Invalid trusted attachment profile")
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
                or type(mime) is not str or not mime
                or entry.get("name") != name
                or entry.get("mime") != mime
            ):
                raise ValueError("Invalid upload identity metadata")
            size = info.get("size")
            checksum = info.get("checksum_sha256") or info.get("hash")
            if (
                type(size) is not int or not 0 < size
                or type(checksum) is not str
                or not _SHA_PATTERN.fullmatch(checksum)
                or entry.get("size") != size
                or entry.get("checksum_sha256") != checksum
            ):
                raise ValueError("Invalid uploaded snapshot")

            if (
                profile == _IMAGE_PROFILE
                and _image_upload(
                    name, mime,
                    max_chat_upload_bytes
                    if max_chat_upload_bytes is not None
                    else DEFAULT_CHAT_UPLOAD_MAX_BYTES,
                )
            ):
                kind = "image"
                max_bytes = (
                    max_chat_upload_bytes
                    if max_chat_upload_bytes is not None
                    else DEFAULT_CHAT_UPLOAD_MAX_BYTES
                )
            elif (
                Path(name).suffix.lower() in _TEXT_EXTENSIONS
                and mime in _TEXT_MIMES
            ):
                kind = "text"
                max_bytes = _MAX_BYTES_PER_TEXT_FILE
            else:
                raise ValueError("Unsupported admitted attachment type")
            if size > max_bytes:
                raise ValueError("Uploaded attachment exceeds the profile limit")

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
                if kind == "text":
                    payload = handle.read(_MAX_BYTES_PER_TEXT_FILE + 1)
                    if len(payload) != size or hashlib.sha256(payload).hexdigest() != checksum:
                        raise ValueError("Upload content changed")
                    if b"\x00" in payload:
                        raise ValueError("Binary content is not supported by this text profile")
                    payload.decode("utf-8")
                else:
                    if _hash_file(path) != checksum:
                        raise ValueError("Upload content changed")
            resolved.append(AdmittedTextAttachment(
                attachment_id=attachment_id, name=name, mime=mime, path=path,
                size=size, checksum_sha256=checksum,
                device=file_stat.st_dev, inode=file_stat.st_ino,
                kind=kind,
            ))
        return tuple(resolved)
    except AdmissionDenied:
        raise
    except Exception as exc:
        # Never reflect filenames, raw content, local paths or index secrets.
        raise AdmissionDenied(409, "Selected attachment is unavailable") from exc


def resolve_admitted_text_attachments(
    *, attachment_ids: tuple[str, ...], owner: str | None,
    upload_handler: Any, ports: Any,
) -> tuple[AdmittedTextAttachment, ...]:
    """Backward-compatible text-only wrapper for direct turns."""
    return resolve_admitted_attachments(
        attachment_ids=attachment_ids, owner=owner,
        upload_handler=upload_handler, ports=ports,
        profile=_TEXT_PROFILE,
    )
