"""M3 live acceptance: the installed mnerys binary against a real uvicorn.

Builds the CLI wheel, installs it into a fresh venv, boots the Gateway
fixture server on loopback, and drives the installed binary over real
HTTP: chat streaming, discovery, doctor, auth failure and cancellation.
"""

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "packages" / "mnerys-cli"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


@pytest.fixture(scope="module")
def live_env(tmp_path_factory):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required for live CLI acceptance")
    root = tmp_path_factory.mktemp("live")
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
        [sys.executable, "-m", "uvicorn", "tests.remote_gateway.cli_fixture_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
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
            "bin_dir": str(bin_dir), "root": root,
        }
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _cli(live_env, *args, token: str | None = "test-token",
         env=None, timeout=120, check=True):
    merged = {"MNERYS_ENDPOINT": live_env["endpoint"]}
    if token is not None:
        merged["MNERYS_TOKEN"] = token
    if env:
        merged.update(env)
    return _run(
        [live_env["entry"], *args], live_env["root"], env=merged,
        timeout=timeout, check=check,
    )


def test_live_chat_streams_the_answer(live_env):
    result = _cli(live_env, "chat", "hello", "--session", "session-1")
    assert result.returncode == 0, result.stderr
    assert "Olá, CLI!" in result.stdout


def test_live_chat_writes_unicode(live_env):
    result = _cli(live_env, "chat", "Olá, mundo 你好", "--session", "session-1")
    assert result.returncode == 0, result.stderr
    assert "Olá, CLI!" in result.stdout


def test_live_discovery_commands(live_env):
    models = _cli(live_env, "models", "list")
    assert models.returncode == 0
    assert "m1" in models.stdout
    tools = _cli(live_env, "tools", "list")
    assert "manage_notes" in tools.stdout
    agents = _cli(live_env, "agents", "list")
    assert agents.returncode == 0


def test_live_doctor_reports_ok(live_env):
    result = _cli(live_env, "doctor")
    assert result.returncode == 0, result.stderr
    assert "doctor: OK" in result.stdout
    assert "models visible" in result.stdout


def test_live_invalid_token_is_rejected_with_exit_4(live_env):
    result = _cli(live_env, "chat", "hello", "--session", "session-1",
                  token="wrong-token", check=False)
    assert result.returncode == 4, result.stderr
    assert "rejected" in result.stderr or "401" in result.stderr


def test_live_cancellation_requests_the_exact_turn(live_env):
    """A stuck stream interrupted by SIGINT cancels the exact turn."""
    if os.name == "nt":
        pytest.skip("SIGINT delivery is POSIX-specific")
    process = subprocess.Popen(
        [live_env["entry"], "chat", "wait-turn", "--session", "session-1"],
        cwd=live_env["root"],
        env={
            **os.environ,
            "PYTHONPATH": str(REPO),
            "MNERYS_ENDPOINT": live_env["endpoint"],
            "MNERYS_TOKEN": "test-token",
        },
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(3.0)
    process.send_signal(signal.SIGINT)
    _stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 5, stderr  # the CLI maps interrupted runs to exit 5
    assert "[cancelled]" in stderr or "cancelled" in stderr.lower()


def test_live_json_output_after_completion(live_env):
    result = _cli(live_env, "chat", "hello", "--session", "session-1", "--json")
    assert result.returncode == 0, result.stderr
    assert '"turn_id"' in result.stdout
    assert '"status"' in result.stdout


def test_local_serve_and_local_chat_share_the_gateway_contract(live_env, tmp_path):
    """mnerys serve boots the local runtime; local chat talks to it."""
    (tmp_path / "app.py").write_text(
        "from tests.remote_gateway.cli_fixture_server import app\n",
        encoding="utf-8",
    )
    port = _free_port()
    serve = subprocess.Popen(
        [live_env["entry"], "serve", "--port", str(port)],
        cwd=live_env["root"],
        env={
            **os.environ,
            "MNERYS_APP_DIR": str(tmp_path),
            "MNERYS_APP_PYTHON": sys.executable,
            "PYTHONPATH": str(REPO),
        },
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(40):
            import urllib.request
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2,
                ) as response:
                    if response.status == 200:
                        ready = True
                        break
            except OSError:
                pass
            time.sleep(0.5)
        assert ready, "mnerys serve did not become ready"
        local = _cli(
            live_env, "chat", "hello", "--session", "session-1",
            env={"MNERYS_ENDPOINT": f"http://127.0.0.1:{port}"},
        )
        assert local.returncode == 0, local.stderr
        assert "Olá, CLI!" in local.stdout
    finally:
        serve.terminate()
        try:
            serve.wait(timeout=10)
        except subprocess.TimeoutExpired:
            serve.kill()


def test_serve_refuses_without_a_local_runtime(live_env):
    result = _cli(
        live_env, "serve", "--port", "7999", token=None,
        env={"MNERYS_APP_DIR": ""},
        check=False,
    )
    assert result.returncode == 3
    assert "MNERYS_APP_DIR" in result.stderr