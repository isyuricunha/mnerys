"""M3 packaging acceptance: the mnerys console script installs cleanly.

Builds the mnerys-cli wheel and sdist with uv, installs the wheel into a
fresh user-level venv outside the repository, and runs the installed
entry point. Nothing here touches the preview or any real server.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import uuid

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
    python = sys.executable
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
    version = _run([str(entry), "--version"], tmp_path)
    assert version.returncode == 0
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