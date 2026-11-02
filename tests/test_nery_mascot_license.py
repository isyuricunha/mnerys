"""Regression tests for separate proprietary Nery Mizuki artwork licensing.

This verifies documentation and metadata contracts only, not legal enforceability.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE = ROOT / "assets/mascot/LICENSE.md"
LICENSE_REF = "LicenseRef-Nery-Mizuki-Proprietary-1.0"


def test_notice_exists_and_is_separate_from_agpl():
    notice = LICENSE.read_text(encoding="utf-8")
    assert "Copyright © 2026 Yuri Cunha" in notice
    assert "All rights reserved" in notice
    assert "install, run, display, cache and privately back up" in notice
    assert "only the rights holder" in notice.lower()
    assert "static/icons/nery-mizuki-avatar.webp" in notice
    assert "assets/mascot/kit-v1/" in notice
    assert "AGPL-3.0 grant for code does not extend" in notice
    assert "geometric M logo" in notice
    assert (ROOT / "LICENSE").read_text().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")


def test_mascot_and_kit_manifest_reference_same_notice():
    mascot = json.loads((ROOT / "assets/mascot/mascot.json").read_text(encoding="utf-8"))
    kit = json.loads((ROOT / "assets/mascot/kit-v1/manifest.json").read_text(encoding="utf-8"))
    assert mascot["license"] == kit["license"] == LICENSE_REF
    assert mascot["licenseFile"] == "LICENSE.md"
    assert kit["license_file"] == "../LICENSE.md"
    assert kit["rights_holder"] == "Yuri Cunha"
    assert (ROOT / "assets/mascot" / mascot["licenseFile"]).resolve() == LICENSE.resolve()
    assert (ROOT / "assets/mascot/kit-v1" / kit["license_file"]).resolve() == LICENSE.resolve()


def test_public_facing_docs_explain_reuse_and_redistribution():
    files = [
        "README.md",
        "docs/mascot/README.md",
        "docs/mascot/KIT.md",
        "docs/mascot/ARTWORK_STATUS.md",
        "docs/mascot/KIT_IMPORT.md",
        "docs/branding/README.md",
    ]
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "assets/mascot/LICENSE.md" in text or "../../assets/mascot/LICENSE.md" in text, name
    assert "AGPL" in (ROOT / "README.md").read_text()
    assert "Nery Mizuki" not in (ROOT / "static/icons/mnerys-icon.svg").read_text()
