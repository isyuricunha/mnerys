"""Tests for the native Mnerys preview smoke script (no network access)."""
import importlib.util
from pathlib import Path
from urllib import error
import io
import json
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_native_preview.py"
spec = importlib.util.spec_from_file_location("preview_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class StubResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size=None):
        return self.body if size is None else self.body[:size]


def test_authenticated_native_preview_smoke(monkeypatch):
    paths = []
    def urlopen(req, timeout=10):
        path = req.full_url.rsplit("/", 1)[-1]
        paths.append(path)
        if path == "health":
            return StubResponse(200, b'{"status":"healthy"}')
        if path == "login":
            return StubResponse(200, b"<title>Mnerys</title>")
        raise error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(smoke.request, "urlopen", urlopen)
    assert smoke.check_preview("http://example.com:52525") == {
        "health": "ok", "login": "ok", "readiness_auth": "enforced"
    }
    assert paths == ["health", "login", "ready"]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://username:password@example.com",
    "http://example.com/secret",
    "example.com",
])
def test_rejects_bad_base_urls(url):
    with pytest.raises(ValueError):
        smoke.check_preview(url)


def test_native_preview_docs_do_not_expose_internal_network_addresses():
    doc = (ROOT / "docs/deployment/NATIVE_PREVIEW.md").read_text(encoding="utf-8")
    assert "<TAILSCALE_HOST>" in doc and "<PREVIEW_PORT>" in doc
    assert "systemd" in doc
    assert "zero downtime" in doc.lower()
    assert "100.78." not in doc
    assert "/home/" not in doc
