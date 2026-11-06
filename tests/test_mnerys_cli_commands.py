"""M3 CLI command behavior: profiles, credentials, errors and streaming.

Uses an httpx MockTransport for the Gateway protocol; the live-process
acceptance against a real uvicorn server lives in
test_mnerys_cli_live_gateway.py.
"""

import json

import httpx
import pytest

from mnerys_cli import commands
from mnerys_cli.client import GatewayClient
from mnerys_cli.config import load_profiles, profile_paths, save_profile
from mnerys_cli.errors import AuthError, ConfigError, MnerysCliError


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok_json(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=data,
        headers={"content-type": "application/json"},
    )


def _sse(items: list[dict]) -> httpx.Response:
    body = "".join(
        f"data: {json.dumps(item, separators=(',', ':'))}\n\n" for item in items
    ) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def test_client_rejects_invalid_endpoints():
    with pytest.raises(MnerysCliError, match="http"):
        GatewayClient("example.com", "t")


def test_client_maps_401_to_auth_error():
    def handler(request):
        return _ok_json({"detail": "rejected"}, status=401)

    client = GatewayClient("http://gateway.test", "bad", transport=_transport(handler))
    with pytest.raises(AuthError):
        client.models()
    client.close()


def test_client_parses_the_sse_stream_and_marks_done():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return _sse([
            {"sequence": 1, "kind": "message.delta", "payload": {"text": "Olá "}},
            {"sequence": 2, "kind": "message.delta", "payload": {"text": "mundo"}},
            {"sequence": 3, "kind": "turn.completed", "payload": {}},
        ])

    client = GatewayClient("http://gateway.test", "t", transport=_transport(handler))
    items = client.turn_events("abc", after_seq=0)
    assert [item["kind"] for item in items] == [
        "message.delta", "message.delta", "turn.completed",
    ]
    assert items[0]["payload"]["text"] == "Olá "
    client.close()


def test_profile_and_credentials_are_stored_restrictively(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    save_profile("default", "https://gateway.example.com", "secret-token")
    root = profile_paths()[0].parent
    profiles_file = root / "profiles.json"
    assert profiles_file.is_file()
    assert (profiles_file.stat().st_mode & 0o777) == 0o600
    token_file = root / "credentials" / "default.token"
    assert token_file.read_text(encoding="utf-8") == "secret-token"
    assert (token_file.stat().st_mode & 0o777) == 0o600
    data = load_profiles()
    assert data["active"] == "default"
    assert data["profiles"]["default"]["endpoint"] == "https://gateway.example.com"


def test_credentials_are_not_contaminated_by_profile_json(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    save_profile("default", "https://gateway.example.com", "hunter2")
    raw = (tmp_path / "cfg" / "mnerys" / "profiles.json").read_text(encoding="utf-8")
    assert "hunter2" not in raw


def test_resolve_endpoint_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    save_profile("default", "https://gateway.example.com", None)
    monkeypatch.setenv("MNERYS_ENDPOINT", "http://127.0.0.1:7021")
    monkeypatch.setenv("MNERYS_TOKEN", "env-token")
    endpoint, token = commands._client_for.__globals__[
        "resolve_endpoint"
    ](load_profiles(), None)
    assert endpoint == "http://127.0.0.1:7021"
    assert token == "env-token"


def test_missing_profile_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("MNERYS_ENDPOINT", raising=False)
    monkeypatch.delenv("MNERYS_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="connect"):
        commands._client_for.__globals__["resolve_endpoint"](load_profiles(), None)


def test_chat_streams_text_until_completed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    endpoints: list[str] = []

    def handler(request):
        endpoints.append(request.url.path)
        if request.url.path == "/api/v1/sessions":
            return _ok_json({"sessions": [{"id": "s1", "name": "n"}]})
        if request.url.path == "/api/v1/turns":
            return _ok_json({"turn_id": "t1", "session_id": "s1", "status": "running"}, status=201)
        if request.url.path == "/api/v1/turns/t1/events":
            return _sse([
                {"sequence": 1, "kind": "message.delta", "payload": {"text": "4"}},
                {"sequence": 2, "kind": "turn.completed", "payload": {}},
            ])
        return _ok_json({}, status=404)

    save_profile("default", "http://gateway.test", "t")
    monkeypatch.setattr(
        "mnerys_cli.commands.GatewayClient",
        lambda *a, **k: GatewayClient(*a, transport=_transport(handler), **{x: k[x] for x in k if x != "transport"}),
    )
    args = _args(chat=True, message="2+2", session="s1", profile=None, mode="chat",
                 idempotency_key=None, json=False)
    assert commands.cmd_chat(args) == 0
    out = capsys.readouterr().out
    assert "4" in out
    assert any(p == "/api/v1/turns" for p in endpoints)


def _args(**overrides):
    import argparse
    namespace = argparse.Namespace()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace