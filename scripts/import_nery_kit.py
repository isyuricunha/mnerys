#!/usr/bin/env python3
"""Verify and import Nery Mizuki asset kit v1 without touching Mnerys branding.

Usage: python3 scripts/import_nery_kit.py /path/to/nery-mizuki-kit-v1.zip [--apply]
Without --apply, the command only checks the archive and reports planned changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

SHA256_V1 = "ec4ee4a77f1253d9b074e55ea8d2f0c138ca58fad34f6fa75f0fd35a846cb657"
MAX_ZIP_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_BYTES = 20 * 1024 * 1024
ASSET_FOLDERS = {"source", "transparent", "avatars", "preview"}


def planned_entries(archive: zipfile.ZipFile, repo: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    entries = []
    total = 0
    for item in archive.infolist():
        if item.is_dir():
            continue
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
            raise ValueError("Unsafe archive path: " + item.filename)
        ftype = (item.external_attr >> 16) & 0o170000
        if ftype not in (0, 0o100000):
            raise ValueError("Unexpected archive file type: " + item.filename)
        total += item.file_size
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError("Archive expanded-size limit exceeded")
        if path.parts[0] in ASSET_FOLDERS:
            target = repo / "assets" / "mascot" / "kit-v1" / str(path)
        elif item.filename == "manifest.json":
            target = repo / "assets" / "mascot" / "kit-v1" / "manifest.json"
        elif item.filename == "README.md":
            target = repo / "docs" / "mascot" / "KIT.md"
        elif item.filename == "install_into_repo.py":
            # Archived code is never installed or executed.
            continue
        else:
            raise ValueError("Unexpected kit entry: " + item.filename)
        entries.append((item, target))
    if len(entries) != 34:
        raise ValueError(f"Expected 34 files in kit, found {len(entries)}")
    return entries


def import_kit(archive_path: Path, repo: Path, *, apply: bool = False) -> dict:
    archive_path = archive_path.expanduser().resolve()
    repo = repo.expanduser().resolve()
    if not (repo / ".git").is_dir() or not (repo / "assets" / "mascot").is_dir():
        raise ValueError("Target is not a Mnerys checkout with assets/mascot")
    if archive_path.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("Kit ZIP is larger than expected")
    with archive_path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != SHA256_V1:
        raise ValueError("ZIP SHA-256 mismatch: use the reviewed Nery kit v1")
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC verification failed")
        entries = planned_entries(archive, repo)
        new = []
        for item, target in entries:
            payload = archive.read(item)
            # Do not follow symlinks in pre-existing output paths.
            for parent in (target, *target.parents):
                if parent == repo.parent:
                    break
                if parent.is_symlink():
                    raise ValueError("Refusing symlink destination: " + str(parent))
            if target.exists():
                if target.is_dir() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(payload).digest():
                    raise FileExistsError("Refusing to overwrite asset: " + str(target))
            else:
                new.append((target, payload))
        if apply:
            for target, payload in new:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
    return {
        "archive_sha256": digest,
        "total_files": len(entries),
        "new_files": len(new),
        "installed": bool(apply),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify/import approved Nery Mizuki kit v1")
    parser.add_argument("zip_file", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true", help="Actually copy files (default: dry-run)")
    args = parser.parse_args()
    print(json.dumps(import_kit(args.zip_file, args.repo, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
