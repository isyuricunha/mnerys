"""Mnerys GitHub Pages landing: identity, provenance, and honest status.

These checks cover the committed static source. The visual browser capture
suite separately verifies layout and navigation at desktop/mobile widths.
"""

from html.parser import HTMLParser
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
INDEX = WEBSITE / "index.html"
CSS = WEBSITE / "site.css"

LEGACY_VIDEOS = (
    "chat.webm",
    "research.webm",
    "compare.webm",
    "document.webm",
    "notes.webm",
    "gallery.webm",
    "theme.webm",
    "bg.webm",
)


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []
        self.ids = set()
        self.headings = []

    def handle_starttag(self, tag, attrs):
        props = dict(attrs)
        if props.get("id"):
            assert props["id"] not in self.ids, f"duplicate id {props['id']}"
            self.ids.add(props["id"])
        if tag == "a":
            self.links.append(props.get("href"))
        if tag == "img":
            self.images.append(props)
        if tag in ("h1", "h2", "h3"):
            self.headings.append(tag)


def load():
    text = INDEX.read_text(encoding="utf-8")
    parsed = DocumentParser()
    parsed.feed(text)
    return text, parsed


def test_landing_has_mnerys_identity_and_no_upstream_marketing():
    html, doc = load()
    config = (WEBSITE / "_config.yml").read_text(encoding="utf-8")
    assert "<title>Mnerys — Built to be yours.</title>" in html
    assert 'name="theme-color" content="#17191C"' in html
    assert 'href="assets/mnerys-icon.svg"' in html
    assert "title: Mnerys" in config
    assert "Odysseus" not in html
    assert "odysseus-dev/" not in html
    assert "Testimonials" not in html
    assert "Generic Corporate" not in html
    assert doc.headings.count("h1") == 1


def test_feature_and_roadmap_status_remain_truthful():
    html, _ = load()
    assert "Implemented" in html
    assert "M2 complete" in html
    assert "Complete" in html and "Planned" in html
    assert "CENTRAL AUTHORITY / IMPLEMENTED" in html
    assert "SHARED RUNTIME / IMPLEMENTED" in html
    assert "not shipped yet" not in html
    assert "Inherited foundation" not in html
    assert "This is a static screenshot, not a live session" in html
    assert "Gateway" in html and "CLI" in html and "TUI" in html
    assert "Telegram" in html
    assert "private preview" in html


def test_local_page_assets_and_anchor_links_are_valid():
    _, parsed = load()
    for href in parsed.links:
        assert href
        if href.startswith("#"):
            assert href[1:] in parsed.ids, f"broken anchor: {href}"
        elif not href.startswith(("https://", "mailto:")):
            assert (WEBSITE / href.split("#")[0]).is_file(), href
        else:
            assert href.startswith("https://github.com/isyuricunha/mnerys"), (
                f"unexpected off-site link: {href}"
            )
    for img in parsed.images:
        assert img.get("alt") is not None
        assert (WEBSITE / img["src"]).is_file(), img["src"]
    assert (WEBSITE / "site.css").is_file()
    assert (WEBSITE / "assets/mnerys-eclipse-login-desktop.png").read_bytes() == (
        ROOT / "docs/screenshots/mnerys-eclipse-login-desktop.png"
    ).read_bytes()
    assert (WEBSITE / "assets/mnerys-icon.svg").read_bytes() == (
        ROOT / "static/icons/mnerys-icon.svg"
    ).read_bytes()


def test_eclipse_palette_accessibility_and_motion_preferences():
    css = CSS.read_text(encoding="utf-8")
    for color in ("#17191c", "#0f1113", "#3c4248", "#46c2b3", "#e8e4dc"):
        assert color in css.lower()
    assert "prefers-reduced-motion:reduce" in css
    assert ":focus-visible" in css
    assert "@media(max-width:510px)" in css
    assert "nav-links.is-open" in css


def test_upstream_video_archive_is_preserved_without_current_page_embeds():
    html, _ = load()
    archive = (WEBSITE / "legacy-previews.md").read_text(encoding="utf-8")
    for name in LEGACY_VIDEOS:
        assert name not in html
        assert name in archive
        assert (WEBSITE / name).is_file(), name
    assert "<video" not in html.lower()
    assert "historical" in archive.lower()
    assert re.search(r"not.+current Mnerys", archive, re.I)
