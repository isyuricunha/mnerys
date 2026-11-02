"""Contract tests for Mnerys public-facing branding.

Legacy Odysseus identifiers remain intentionally unchanged where they are
storage, API, and deployment compatibility contracts.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_public_app_identity() -> None:
    index = (STATIC / "index.html").read_text()
    login = (STATIC / "login.html").read_text()
    assert "<title>Mnerys — Built to be yours</title>" in index
    assert "<title>Mnerys — Login</title>" in login
    assert 'class="sidebar-brand-title"' in index
    assert "Mnerys</span>" in index
    assert "Built to be yours." in index
    assert "Built to be yours." in login
    assert "Message Odysseus" not in index
    assert "Odysseus Chat" not in index


def test_branding_assets_and_manifest() -> None:
    manifest = json.loads((STATIC / "manifest.json").read_text())
    assert manifest["name"] == manifest["short_name"] == "Mnerys"
    assert manifest["theme_color"] == "#0d1022"
    for icon in manifest["icons"]:
        path = STATIC / icon["src"]
        assert path.is_file(), str(path)
        assert path.read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a"))

    for path in [
        STATIC / "icons/mnerys-icon.svg",
        ROOT / "assets/branding/mnerys-wordmark-dark.svg",
        ROOT / "assets/branding/mnerys-wordmark-light.svg",
        ROOT / "assets/branding/mnerys-hero.svg",
    ]:
        assert ET.parse(path).getroot().tag.endswith("svg")

    assert (STATIC / "icon.ico").read_bytes()[:4] == bytes.fromhex("00000100")


def test_preview_screenshots_are_real_files_referenced_by_readme() -> None:
    readme = (ROOT / "README.md").read_text()
    for filename in (
        "mnerys-workspace-desktop.png",
        "mnerys-workspace-mobile.png",
        "mnerys-login.png",
    ):
        path = ROOT / "docs" / "screenshots" / filename
        assert path.is_file()
        assert path.read_bytes().startswith(bytes.fromhex("89504e470d0a1a0a"))
        assert f"docs/screenshots/{filename}" in readme


def test_official_mascot_assets_and_identity() -> None:
    avatar = ROOT / "assets" / "mascot" / "nery-mizuki-avatar.webp"
    metadata = ROOT / "assets" / "mascot" / "mascot.json"
    guide = ROOT / "docs" / "mascot" / "README.md"
    assert avatar.is_file() and avatar.stat().st_size > 2_000
    raw = avatar.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    data = json.loads(metadata.read_text())
    assert data["name"] == "Nery Mizuki"
    assert data["project"] == "Mnerys"
    assert "Nery Mizuki" in guide.read_text()
    readme = (ROOT / "README.md").read_text()
    assert "Nery Mizuki" in readme
    assert "assets/mascot/nery-mizuki-avatar.webp" in readme
    assert "docs/mascot/README.md" in readme


def test_internal_storage_and_api_identifiers_are_unchanged() -> None:
    # The visual rebrand must not silently erase existing browser preferences.
    theme_js = (STATIC / "js/theme.js").read_text()
    storage_js = (STATIC / "js/storage.js").read_text()
    assert "odysseus-theme" in theme_js
    assert "odysseus-theme" in storage_js
    assert "mnerys-v1-branding" in (STATIC / "sw.js").read_text()
    # Run ID HTTP header is intentionally unchanged until API migration.
    assert "X-Odysseus-Run-Id" in (STATIC / "js/chat.js").read_text()
