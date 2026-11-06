"""M3 packaging acceptance: the mnerys console script installs cleanly.

Builds the mnerys-cli wheel and sdist with uv, installs the wheel into a
fresh user-level venv outside the repository, and runs the installed
entry point. Nothing here touches the preview or any real server.
"""

import email
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "packages" / "mnerys-cli"


def _run(args, cwd, *, check=True, env=None):
    merged = dict(os.environ)
    merged.pop("PYTHONPATH", None)
    if env:
        merged.update(env)
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=300, env=merged,
        check=check,
    )


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required for packaging acceptance")
    out = tmp_path_factory.mktemp("dist")
    _run([uv, "build", "--out-dir", str(out)], PACKAGE_DIR)
    files = sorted(out.iterdir())
    wheels = [p for p in files if p.suffix == ".whl"]
    sources = [p for p in files if p.name.endswith((".tar.gz", ".zip"))]
    assert wheels and sources, f"build produced: {[p.name for p in files]}"
    return wheels[0], sources[0]


def _fresh_venv(root: Path) -> Path:
    venv = root / f"venv-{uuid.uuid4().hex[:8]}"
    _run([sys.executable, "-m", "venv", str(venv)], root)
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    return bin_dir


def test_wheel_and_sdist_are_produced(built_distributions):
    wheel, sdist = built_distributions
    assert wheel.name.endswith(".whl")
    assert sdist.name.endswith((".tar.gz", ".zip"))


def test_fresh_userlevel_install_exposes_mnerys(tmp_path, built_distributions):
    wheel, _sdist = built_distributions
    bin_dir = _fresh_venv(tmp_path)
    pip = bin_dir / ("pip" + (".exe" if os.name == "nt" else ""))
    _run([str(pip), "install", "--no-deps", str(wheel)], tmp_path)
    entry = bin_dir / ("mnerys" + (".exe" if os.name == "nt" else ""))
    assert entry.is_file(), f"console script missing: {entry}"
    version = _run([str(entry), "--version"], tmp_path, check=False)
    assert version.returncode == 0, f"stderr: {version.stderr!r}"
    assert version.stdout.strip().startswith("mnerys ")
    help_out = _run([str(entry), "--help"], tmp_path)
    assert "connect" in help_out.stdout and "chat" in help_out.stdout
    bare = _run([str(entry)], tmp_path)
    assert bare.returncode == 0 and "usage" in bare.stdout.lower()


def test_remote_only_install_brings_no_server_stack(tmp_path, built_distributions):
    """The CLI wheel must not drag the web/db/scheduler stack along."""
    wheel, _sdist = built_distributions
    bin_dir = _fresh_venv(tmp_path)
    pip = bin_dir / ("pip" + (".exe" if os.name == "nt" else ""))
    _run([str(pip), "install", str(wheel)], tmp_path)
    site = bin_dir.parent / "lib"
    site_packages = next(site.glob("python*/site-packages"))
    installed = {p.name for p in site_packages.iterdir() if p.is_dir()}
    assert "mnerys_cli" in installed
    assert "httpx" in installed
    for weapon in ("fastapi", "uvicorn", "sqlalchemy", "pydantic"):
        assert weapon not in installed, f"server dependency leaked: {weapon}"
    # The installed module must not import the repository tree.
    exe = "python" + (".exe" if os.name == "nt" else "")
    probe = _run(
        [str(bin_dir / exe), "-c",
         ("import mnerys_cli.cli as c; import sys; "
          "assert not any('ella-repos' in p for p in sys.path), sys.path")],
        tmp_path,
    )
    assert probe.returncode == 0, probe.stderr


def test_clean_uninstall_removes_the_console_script(tmp_path, built_distributions):
    wheel, _sdist = built_distributions
    bin_dir = _fresh_venv(tmp_path)
    pip = bin_dir / ("pip" + (".exe" if os.name == "nt" else ""))
    _run([str(pip), "install", "--no-deps", str(wheel)], tmp_path)
    entry = bin_dir / ("mnerys" + (".exe" if os.name == "nt" else ""))
    _run([str(pip), "uninstall", "-y", "mnerys-cli"], tmp_path)
    assert not entry.exists(), "console script survived the uninstall"


# ---------------------------------------------------------------------------
# Unified version policy: the release tag is the only version authority.
# ---------------------------------------------------------------------------


def _wheel_core_metadata(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        message = email.message_from_bytes(archive.read(metadata_name))
    return message["Name"], message["Version"]


def _sdist_pkginfo_version(sdist: Path) -> str:
    with tarfile.open(sdist) as archive:
        member = next(
            entry for entry in archive.getmembers()
            if entry.isfile()
            and entry.name.count("/") == 1
            and entry.name.endswith("/PKG-INFO")
        )
        reader = archive.extractfile(member)
        assert reader is not None
        message = email.message_from_bytes(reader.read())
    return message["Version"]


def test_wheel_and_sdist_metadata_agree(built_distributions):
    wheel, sdist = built_distributions
    name, wheel_version = _wheel_core_metadata(wheel)
    assert name == "mnerys-cli"
    assert wheel_version == _sdist_pkginfo_version(sdist)


def test_repository_tree_builds_are_identifiable_development_builds(
    built_distributions,
):
    """A build from the working tree must never claim a released version."""
    _wheel, sdist = built_distributions
    version = _sdist_pkginfo_version(sdist)
    assert re.match(r"^\d+\.\d+\.\d+\.dev\d+\+g[0-9a-f]+", version), (
        f"a build from the repository tree must be a development build, got {version!r}"
    )


def test_sdist_rebuilds_the_same_version_without_git(built_distributions, tmp_path):
    """Extract the sdist away from any repository and rebuild the wheel."""
    _wheel, sdist = built_distributions
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required for packaging acceptance")
    extracted = tmp_path / "sdist-root"
    with tarfile.open(sdist) as archive:
        try:
            archive.extractall(extracted, filter="data")
        except TypeError:  # Python without the extraction filter API
            archive.extractall(extracted)
    project = next(extracted.iterdir())
    assert not (project / ".git").exists()
    out = tmp_path / "rebuilt"
    _run([uv, "build", "--wheel", "--out-dir", str(out)], project)
    (rebuilt_wheel,) = out.glob("*.whl")
    name, rebuilt_version = _wheel_core_metadata(rebuilt_wheel)
    assert name == "mnerys-cli"
    assert rebuilt_version == _sdist_pkginfo_version(sdist)


def test_reinstall_preserves_profiles_and_tokens(built_distributions, tmp_path):
    """Upgrading the package must keep profiles, tokens and credentials."""
    wheel, _sdist = built_distributions
    bin_dir = _fresh_venv(tmp_path)
    pip = bin_dir / ("pip" + (".exe" if os.name == "nt" else ""))
    config_root = tmp_path / "xdg-config"
    env = {
        "XDG_CONFIG_HOME": str(config_root),
        "MNERYS_ENDPOINT": "",
        "MNERYS_TOKEN": "",
    }
    _run([str(pip), "install", "--no-deps", str(wheel)], tmp_path, env=env)
    # Create the profile the same way the CLI's own storage layer does.
    from mnerys_cli import config as cli_config

    original_env = {k: os.environ.get(k) for k in env}
    os.environ["XDG_CONFIG_HOME"] = str(config_root)
    try:
        cli_config.save_profile("default", "http://127.0.0.1:9", "tok-upgrade-check")
        profiles_file, creds = cli_config._paths(None)
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    token_path = creds / "default.token"
    assert token_path.read_text(encoding="utf-8") == "tok-upgrade-check"

    _run(
        [str(pip), "install", "--no-deps", "--force-reinstall", str(wheel)],
        tmp_path, env=env,
    )
    assert profiles_file.is_file(), "profiles.json did not survive the upgrade"
    assert token_path.read_text(encoding="utf-8") == "tok-upgrade-check", (
        "the stored API token did not survive the upgrade"
    )
    version = _run([str(bin_dir / "mnerys"), "--version"], tmp_path, env=env)
    assert version.returncode == 0, f"stderr: {version.stderr!r}"
    assert version.stdout.strip().startswith("mnerys ")


def test_verify_script_matches_the_tag_or_fails(tmp_path):
    """The release verifier accepts an exact tag and rejects any drift."""
    import io

    script = REPO / "scripts" / "verify_cli_release.py"
    dist = tmp_path / "dist"
    dist.mkdir()

    wheel_path = dist / "mnerys_cli-0.14.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr(
            "mnerys_cli-0.14.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: mnerys-cli\nVersion: 0.14.1\n",
        )
    sdist_path = dist / "mnerys_cli-0.14.1.tar.gz"
    with tarfile.open(sdist_path, "w:gz") as archive:
        pkginfo = io.BytesIO(
            b"Metadata-Version: 1.0\nName: mnerys-cli\nVersion: 0.14.1\n",
        )
        info = tarfile.TarInfo("mnerys_cli-0.14.1/PKG-INFO")
        info.size = pkginfo.getbuffer().nbytes
        archive.addfile(info, pkginfo)

    ok = _run(
        [sys.executable, str(script), "--tag", "v0.14.1", "--dist-dir", str(dist)],
        tmp_path,
    )
    assert ok.returncode == 0, ok.stderr

    drift = _run(
        [sys.executable, str(script), "--tag", "v9.9.9", "--dist-dir", str(dist)],
        tmp_path, check=False,
    )
    assert drift.returncode == 1, f"drift was accepted: {drift.stdout!r}"

    bad_tag = _run(
        [sys.executable, str(script), "--tag", "not-a-tag", "--dist-dir", str(dist)],
        tmp_path, check=False,
    )
    assert bad_tag.returncode == 1, "non-release tags must be rejected"