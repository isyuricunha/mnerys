"""Resolve the installed mnerys-cli distribution version.

Mnerys has one release version, owned by the Git tags that semantic-release
creates (``vX.Y.Z``). The distribution derives its version from those tags at
build time (hatch-vcs) and this module reads it back from the installed
package metadata, so ``mnerys --version`` always reports the installed
distribution's real version without touching Git at runtime. A missing or
invalid installation fails loudly instead of reporting a wrong version.
"""

from __future__ import annotations

import re
from importlib import metadata

from mnerys_cli.errors import VersionMetadataError

DISTRIBUTION_NAME = "mnerys-cli"

# setuptools-scm emits PEP 440 release versions (X.Y.Z) at release tags and
# X.Y.Z.devN+g<hash> development builds in between, optionally with a dirty
# timestamp appended to the local segment. Accept that family and reject
# placeholders, empty values or corrupted metadata.
_VERSION_PATTERN = re.compile(
    r"^\d+(?:\.\d+)*"
    r"(?:\.dev\d+|\.post\d+|(?:a|b|rc)\d+)*"
    r"(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*(?:\.[a-zA-Z0-9_.-]+)*)?$",
)


def distribution_version() -> str:
    """Return the installed ``mnerys-cli`` version.

    Raises :class:`VersionMetadataError` when the distribution metadata is
    missing or does not carry a plausible version, never a silent
    placeholder.
    """
    try:
        raw = metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError as exc:
        raise VersionMetadataError(
            f"the '{DISTRIBUTION_NAME}' distribution is not installed; "
            "cannot report the CLI version",
        ) from exc
    if not raw or not _VERSION_PATTERN.match(raw):
        raise VersionMetadataError(
            f"installed '{DISTRIBUTION_NAME}' metadata carries an invalid "
            f"version: {raw!r}",
        )
    return raw


__all__ = ["DISTRIBUTION_NAME", "distribution_version"]
