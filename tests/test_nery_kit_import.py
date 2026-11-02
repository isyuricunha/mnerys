"""Safety and integrity checks for the optional Nery mascot ZIP importer."""
import importlib.util
import io
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_nery_kit.py"
spec = importlib.util.spec_from_file_location("nery_kit_importer", SCRIPT)
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


def make_zip(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, value in members.items():
            z.writestr(name, value)
    stream.seek(0)
    return zipfile.ZipFile(stream)


def test_installer_script_is_available():
    assert SCRIPT.exists()
    assert len(kit.SHA256_V1) == 64
    assert kit.SHA256_V1.isascii()


def test_rejects_archive_path_escape(tmp_path):
    with make_zip({"../private.txt": b"unsafe"}) as z, pytest.raises(ValueError, match="Unsafe"):
        kit.planned_entries(z, tmp_path)


def test_rejects_unexpected_zip_entries(tmp_path):
    with make_zip({"secrets.env": b"unsafe"}) as z, pytest.raises(ValueError, match="Unexpected"):
        kit.planned_entries(z, tmp_path)


def test_rejects_partial_archives(tmp_path):
    with make_zip({"transparent/nery-happy.webp": b"dummy"}) as z, pytest.raises(ValueError, match="Expected 34"):
        kit.planned_entries(z, tmp_path)


def test_rejects_zip_with_wrong_digest(tmp_path):
    checkout = tmp_path / "mnerys"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "assets/mascot").mkdir(parents=True)
    path = tmp_path / "fake.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("README.md", "invalid")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        kit.import_kit(path, checkout)
