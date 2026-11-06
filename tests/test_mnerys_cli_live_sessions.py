"""M4.1 live acceptance: the installed CLI creates sessions over real HTTP.

Starts the loopback fixture with an EMPTY session cache, so `mnerys chat`
must create its session through the real Gateway v1 router (the exact latent
gap this submilestone closes). Server-side session state is asserted through
the same public v1 API.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "packages" / "mnerys-cli"


def _run(args, cwd, *, env=None, timeout=300, check=True):
    merged = dict(os.environ)
    merged.pop("PYTHONPATH", None)
    merged["PYTHONPATH"] = str(REPO)
    if env:
        merged.update(env)
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        env=merged, check=check,
    )
    return result


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def empty_live_env(tmp_path_factory):
    """Installed wheel + fixture server with no pre-seeded sessions."""
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required for live CLI acceptance")
    root = tmp_path_factory.mktemp("live-sessions")
    dist = root / "dist"
    _run([uv, "build", "--out-dir", str(dist)], PACKAGE_DIR)
    wheels = list(dist.glob("*.whl"))
    assert wheels
    venv = root / "venv"
    _run([sys.executable, "-m", "venv", str(venv)], root)
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    _run([str(bin_dir / "pip"), "install", str(wheels[0])], root)
    entry = bin_dir / ("mnerys" + (".exe" if os.name == "nt" else ""))
    assert entry.is_file()

    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "tests.remote_gateway.cli_fixture_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO), "FIXTURE_SEED_SESSION": "0"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                probe = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://127.0.0.1:{port}/api/health"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if probe.stdout.strip() == "200":
                    ready = True
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(0.5)
        if not ready:
            server.kill()
            pytest.skip("fixture server did not become ready")
        yield {
            "entry": str(entry), "endpoint": f"http://127.0.0.1:{port}",
            "root": root,
        }
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _cli(empty_live_env, *args, timeout=120, check=True):
    return _run(
        [empty_live_env["entry"], *args], empty_live_env["root"],
        env={
            "MNERYS_ENDPOINT": empty_live_env["endpoint"],
            "MNERYS_TOKEN": "test-token",
        },
        timeout=timeout, check=check,
    )


def _listed_sessions(endpoint: str) -> list[dict]:
    with httpx.Client(
        base_url=endpoint,
        headers={"Authorization": "Bearer test-token"}, timeout=10,
    ) as client:
        return client.get("/api/v1/sessions").json().get("sessions", [])


def test_chat_without_a_session_creates_one_through_the_real_router(
    empty_live_env,
):
    assert _listed_sessions(empty_live_env["endpoint"]) == []
    result = _cli(empty_live_env, "chat", "2+2")
    assert result.returncode == 0, result.stderr
    assert "Olá, CLI!" in result.stdout
    sessions = _listed_sessions(empty_live_env["endpoint"])
    assert len(sessions) == 1
    assert sessions[0]["name"].startswith("cli-")
    assert sessions[0]["model"] == "vendor/m3-flash"


def test_a_repeat_turn_reuses_the_created_session(empty_live_env):
    first = _cli(empty_live_env, "chat", "2+2")
    assert first.returncode == 0, first.stderr
    second = _cli(empty_live_env, "chat", "2+3")
    assert second.returncode == 0, second.stderr
    assert "Olá, CLI!" in second.stdout
    # With one session listed, the CLI reuses it instead of creating another.
    assert len(_listed_sessions(empty_live_env["endpoint"])) == 1
