"""Verify built mnerys-cli distributions against a Mnerys release tag.

Mnerys keeps one unified release version: the semantic-release Git tag
(``vX.Y.Z``) is the only authority. This script reads the core metadata of
every wheel and sdist in a distribution directory and requires each version
to equal the tag version exactly, so a mismatched build can never reach a
GitHub Release. The release workflow and the packaging regression tests
share this implementation.
"""

from __future__ import annotations

import argparse
import email
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import NoReturn

RELEASE_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
DISTRIBUTION_NAME = "mnerys-cli"


def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def tag_version(tag: str) -> str:
    match = RELEASE_TAG.match(tag)
    if not match:
        _fail(f"release tag {tag!r} is not a strict 'vX.Y.Z' tag")
    return match.group("version")


def wheel_core_version(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            _fail(f"{path.name}: wheel has no .dist-info/METADATA member")
        message = email.message_from_bytes(archive.read(metadata_name))
    name = (message.get("Name") or "").strip()
    version = (message.get("Version") or "").strip()
    if not version:
        _fail(f"{path.name}: wheel metadata carries no Version field")
    return name, version


def sdist_core_version(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        member = next(
            (
                entry
                for entry in archive.getmembers()
                if entry.isfile()
                and entry.name.count("/") == 1
                and entry.name.endswith("/PKG-INFO")
            ),
            None,
        )
        if member is None:
            _fail(f"{path.name}: sdist has no root PKG-INFO member")
        reader = archive.extractfile(member)
        if reader is None:
            _fail(f"{path.name}: PKG-INFO member is not readable")
        message = email.message_from_bytes(reader.read())
    version = (message.get("Version") or "").strip()
    if not version:
        _fail(f"{path.name}: sdist PKG-INFO carries no Version field")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that built mnerys-cli distributions match a "
            "Mnerys release tag."
        ),
    )
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.14.1")
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument(
        "--require-sdist", action="store_true",
        help="fail when the directory contains no source distribution",
    )
    args = parser.parse_args(argv)

    expected = tag_version(args.tag)
    dist_dir = args.dist_dir
    if not dist_dir.is_dir():
        _fail(f"distribution directory {dist_dir} does not exist")

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if not wheels:
        _fail(f"{dist_dir} contains no wheel")
    if args.require_sdist and not sdists:
        _fail(f"{dist_dir} contains no sdist and --require-sdist was set")

    failures: list[str] = []
    for wheel in wheels:
        name, version = wheel_core_version(wheel)
        if name != DISTRIBUTION_NAME:
            failures.append(f"{wheel.name}: wheel metadata Name is {name!r}")
        if version != expected:
            failures.append(
                f"{wheel.name}: wheel metadata Version {version!r} != {expected!r}",
            )
        else:
            print(f"ok: {wheel.name}: Version {version}")
    for sdist in sdists:
        version = sdist_core_version(sdist)
        if version != expected:
            failures.append(
                f"{sdist.name}: sdist PKG-INFO Version {version!r} != {expected!r}",
            )
        else:
            print(f"ok: {sdist.name}: Version {version}")

    if failures:
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 1
    print(
        f"verified: {len(wheels)} wheel(s) and {len(sdists)} sdist(s) "
        f"match release {args.tag} (version {expected})",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
