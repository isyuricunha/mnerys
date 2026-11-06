"""Mnerys CLI package metadata.

The release version is owned by the Git tags that semantic-release creates.
The distribution derives it at build time and the package reads it from the
installed distribution metadata; see :mod:`mnerys_cli.version`.
"""

from mnerys_cli.version import distribution_version as distribution_version


def __getattr__(name: str):
    # Kept as a lazily resolved attribute so existing imports of
    # ``mnerys_cli.__version__`` keep working; resolution happens against the
    # installed distribution metadata and fails loudly when it is missing.
    if name == "__version__":
        return distribution_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
