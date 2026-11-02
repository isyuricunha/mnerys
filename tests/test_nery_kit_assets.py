"""Nery Mizuki kit v1: file completeness, provenance and transparent art contracts."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "assets" / "mascot" / "kit-v1"
POSES = {"portrait", "fullbody", "happy", "thinking", "sleeping", "coding", "coffee", "with-cat"}


def test_kit_manifest_and_all_asset_paths_exist():
    meta = json.loads((KIT / "manifest.json").read_text(encoding="utf-8"))
    assert meta["version"] == "1.0"
    assert meta["original_sheet_dimensions"] == [1536, 1024]
    assert meta["contains_upscales"] is False
    assert {a["name"] for a in meta["assets"]} == POSES
    assert len(meta["assets"]) == 8

    for pose in meta["assets"]:
        assert (KIT / pose["output_png"]).is_file()
        assert (KIT / pose["output_webp"]).is_file()
        assert (KIT / "source" / "original-crops" / ("nery-mizuki-" + pose["name"] + ".png")).is_file()

    assert (KIT / "source" / "approved-character-sheet.png").is_file()
    assert (KIT / "preview" / "nery-kit-preview.png").is_file()
    for size in [128, 256, 512]:
        for ext in ("png", "webp"):
            assert (KIT / "avatars" / f"nery-avatar-{size}.{ext}").is_file()
    assert (ROOT / "docs/mascot/KIT.md").is_file()


def test_transparent_assets_have_real_alpha_and_native_dimensions():
    Image = pytest.importorskip("PIL.Image")
    meta = json.loads((KIT / "manifest.json").read_text(encoding="utf-8"))
    for pose in meta["assets"]:
        size = tuple(pose["source_dimensions"])
        assert tuple(pose["transparent_dimensions"]) == size
        for key in ("output_png", "output_webp"):
            with Image.open(KIT / pose[key]) as image:
                image.verify()
            with Image.open(KIT / pose[key]) as image:
                assert image.size == size
                assert "A" in image.getbands()
                lo, hi = image.getchannel("A").getextrema()
                assert lo == 0 and hi == 255

    with Image.open(KIT / "source/approved-character-sheet.png") as sheet:
        assert sheet.size == (1536, 1024)
        assert not sheet.getexif()
    with Image.open(KIT / "transparent/nery-fullbody.png") as fullbody:
        assert fullbody.size == (195, 403)
    for size in (128, 256, 512):
        for ext in ("png", "webp"):
            with Image.open(KIT / "avatars" / f"nery-avatar-{size}.{ext}") as avatar:
                assert avatar.size == (size, size)
                assert "A" in avatar.getbands()


def test_mascot_kit_is_not_application_logomark():
    icon = (ROOT / "static/icons/mnerys-icon.svg").read_text()
    assert "M6 25.2" in icon
    assert "nery" not in icon.lower()

    metadata = json.loads((ROOT / "assets/mascot/mascot.json").read_text(encoding="utf-8"))
    assert metadata["assetStatus"] == "approved-kit-v1"
    assert metadata["role"] == "official mascot"

    # The existing app avatar is deliberately unchanged by installing the kit.
    assert (ROOT / "assets/mascot/nery-mizuki-avatar.webp").read_bytes() == (
        ROOT / "static/icons/nery-mizuki-avatar.webp"
    ).read_bytes()

    readme = (ROOT / "README.md").read_text()
    assert "assets/mascot/kit-v1/preview/nery-kit-preview.png" in readme
    assert "docs/mascot/KIT.md" in readme
