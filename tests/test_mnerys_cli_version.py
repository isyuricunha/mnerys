"""Unified release version policy for the mnerys CLI.

Mnerys has one release version, owned by the semantic-release Git tags. The
distribution must derive it at build time and the CLI must read it from the
installed distribution metadata, failing loudly when the metadata is missing
or invalid. These tests prevent the CLI from reintroducing a hardcoded
version that diverges from the project release.
"""

import argparse
import re
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from mnerys_cli import version as cli_version
from mnerys_cli.errors import VersionMetadataError

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "packages" / "mnerys-cli"


def test_package_pyproject_derives_the_version_from_vcs():
    data = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "version" not in project, (
        "a hardcoded [project] version diverges from the project release"
    )
    assert "version" in project.get("dynamic", [])
    hatch_version = data["tool"]["hatch"]["version"]
    assert hatch_version["source"] == "vcs"
    pattern = hatch_version["tag-pattern"]
    # Only strict vX.Y.Z release tags may drive the distribution version.
    assert pattern.startswith("^v") and pattern.endswith("$")
    assert "(?P<version>" in pattern
    # No silent fallback: builds without Git metadata must fail loudly.
    assert not hatch_version.get("fallback-version")


def test_package_module_carries_no_hardcoded_version():
    for module in sorted((PACKAGE_DIR / "mnerys_cli").glob("*.py")):
        source = module.read_text(encoding="utf-8")
        assert not re.search(r'__version__\s*=\s*["\']', source), (
            f"{module.name} hardcodes the version instead of using metadata"
        )


def test_resolution_accepts_release_and_development_versions():
    versions = (
        "0.14.1",
        "0.15.0",
        "1.2.3rc1",
        "1.2.3.post2",
        "0.14.1.dev7+g3332d6aa",
        "0.14.1.dev7+g3332d6aa.d20261106",
    )
    for candidate in versions:
        with patch.object(cli_version.metadata, "version", return_value=candidate):
            assert cli_version.distribution_version() == candidate


def test_resolution_rejects_invalid_metadata():
    for candidate in (
        "",
        "unknown",
        "SETUPTOOLS_SCM_VERSION_SCHEME",
        "version-zero",
        "not a version",
    ):
        with (
            patch.object(cli_version.metadata, "version", return_value=candidate),
            pytest.raises(VersionMetadataError),
        ):
            cli_version.distribution_version()


def test_resolution_requires_an_installed_distribution():
    def raise_missing(name):
        raise cli_version.metadata.PackageNotFoundError(name)

    with patch.object(cli_version.metadata, "version", side_effect=raise_missing):
        with pytest.raises(VersionMetadataError) as excinfo:
            cli_version.distribution_version()
    assert excinfo.value.exit_code == 6


def test_lazy_dunder_version_matches_the_distribution():
    import mnerys_cli

    with patch.object(cli_version.metadata, "version", return_value="7.7.7"):
        assert mnerys_cli.__version__ == "7.7.7"


def test_unknown_attributes_still_raise():
    import mnerys_cli

    with pytest.raises(AttributeError):
        mnerys_cli.nope  # noqa: B018


def test_cli_version_flag_reports_the_installed_version(capsys):
    from mnerys_cli import cli

    with patch.object(cli, "distribution_version", return_value="9.9.9"):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "mnerys 9.9.9"


def test_cli_version_flag_fails_safely_without_metadata(capsys):
    from mnerys_cli import cli

    with (
        patch.object(
            cli, "distribution_version", side_effect=VersionMetadataError("broken install"),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.main(["--version"])
    assert excinfo.value.code == 6
    captured = capsys.readouterr()
    assert "broken install" in captured.err
    assert captured.out == ""


def test_cli_help_survives_a_broken_installation(capsys):
    from mnerys_cli import cli

    with (
        patch.object(
            cli, "distribution_version", side_effect=VersionMetadataError("broken install"),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_doctor_reports_a_broken_install_without_crashing(capsys, tmp_path, monkeypatch):
    from mnerys_cli import commands

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("MNERYS_ENDPOINT", raising=False)
    with patch.object(
        commands, "distribution_version", side_effect=VersionMetadataError("broken install"),
    ):
        code = commands.cmd_doctor(argparse.Namespace(profile=None))
    out = capsys.readouterr().out
    assert "mnerys cli version: unavailable" in out
    assert "broken install" in out
    assert code in (1, 3)
