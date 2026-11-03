"""Visual brand regression tests, including preserved Odysseus themes."""
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
THEME = (STATIC / "js/theme.js").read_text()
STORAGE = (STATIC / "js/storage.js").read_text()


def test_inherited_theme_palettes_unchanged():
    original = json.loads((ROOT / "tests/fixtures/odysseus_original_themes.json").read_text())
    assert len(original) == 19
    for name, colors in original.items():
        target = "classic_dark" if name == "dark" else name
        m = re.search(r"^\s*" + re.escape(target) + r":\s*\{([^\}]+)", THEME, re.M)
        assert m, f"Original theme is missing: {name}"
        for key, value in colors.items():
            expected = rf"\b{re.escape(key)}:\s*'{re.escape(value)}'"
            assert re.search(expected, m.group(1)), (name, key, value)
    assert "const DEFAULT_THEME = 'eclipse'" in THEME
    assert "classic_dark: 'Classic Dark'" in THEME
    assert "dark: 'Mnerys Terracotta'" in THEME


def test_custom_theme_storage_remains_compatible():
    assert "const LS_KEY = 'odysseus-theme'" in THEME
    assert "const CUSTOM_THEMES_KEY = 'odysseus-custom-themes'" in THEME
    assert "THEME: 'odysseus-theme'" in STORAGE


def test_mnerys_logo_is_neutral_and_mascot_is_separate():
    index = (STATIC / "index.html").read_text()
    login = (STATIC / "login.html").read_text()
    for markup in (index, login):
        assert "M6 25.2" in markup
        assert "Nery Mizuki" in markup
        assert "M16 2 L19.2" not in markup
    svg = STATIC / "icons/mnerys-icon.svg"
    ET.parse(svg)
    assert "M6 25.2" in svg.read_text()


def test_eclipse_default_while_terracotta_remains_optional():
    css = (STATIC / "css/00-tokens.css").read_text()
    index = (STATIC / "index.html").read_text()
    login = (STATIC / "login.html").read_text()
    default_colors = {"bg": "#17191c", "fg": "#e8e4dc", "panel": "#0f1113",
                      "border": "#3c4248", "red": "#46c2b3"}
    theme_def = re.search(r"^  eclipse:\s*\{([^}]+)\}", THEME, re.M)
    assert theme_def
    for key, value in default_colors.items():
        assert re.search(rf"\b{key}:'{value}'", theme_def.group(1))
        assert f"--{'red' if key == 'red' else key}: {value};" in css
        assert f"--{'red' if key == 'red' else key}: {value};" in login
    assert "eclipse:    'starfield-depth'" in THEME
    assert "dark:       'none'" in THEME
    assert "applyBgPattern(defaultPattern)" in THEME
    assert "save(DEFAULT_THEME, colors, {" in THEME  # reset survives server-side theme sync
    assert "THEME_DEFAULT_PATTERN[(saved && saved.name) || DEFAULT_THEME]" in THEME
    assert "var name = (t && t.name) || 'eclipse'" in login
    assert "THEME_DEFAULT_PATTERN.eclipse" in login
    assert 'content="#17191c"' in index
    assert "dark:       { bg:'#15110f'" in THEME
    manifest = json.loads((STATIC / "manifest.json").read_text())
    assert manifest["theme_color"].lower() == "#17191c"
    assert manifest["background_color"].lower() == "#17191c"
    sw = (STATIC / "sw.js").read_text()
    assert "mnerys-v4-eclipse-default" in sw
    assert "/static/icons/nery-mizuki-avatar.webp" in sw
    assert (STATIC / "icons/nery-mizuki-avatar.webp").read_bytes() == (
        ROOT / "assets/mascot/nery-mizuki-avatar.webp"
    ).read_bytes()


def test_screenshots_are_real_png_files():
    for name in ("mnerys-workspace-desktop.png", "mnerys-workspace-mobile.png", "mnerys-login.png",
                 "mnerys-eclipse-login-desktop.png", "mnerys-eclipse-login-mobile.png"):
        path = ROOT / "docs/screenshots" / name
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
