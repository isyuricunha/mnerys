"""Deployment runner guard: migration-risk revisions require an attested plan."""

from pathlib import Path

import pytest

from scripts.deploy_native_preview import DeployError, migration_guard

REPO = Path(__file__).resolve().parents[1]
_OLD_REV = "99a065e3182e"  # the M1 baseline release revision (schema-sensitive delta)
_TARGET = "f548d2cc"  # the M2 published revision


def _fake_old_release(tmp_path: Path) -> Path:
    release = tmp_path / _OLD_REV
    release.mkdir()
    (release / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    return release


def test_migration_guard_refuses_without_an_attested_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("MNERYS_DEPLOY_MIGRATION_PLAN_APPROVED", raising=False)
    with pytest.raises(DeployError, match="reviewed migration"):
        migration_guard(REPO, _fake_old_release(tmp_path), _TARGET)


def test_migration_guard_passes_with_the_operator_attestation(tmp_path, monkeypatch):
    monkeypatch.setenv("MNERYS_DEPLOY_MIGRATION_PLAN_APPROVED", "1")
    # The M2 delta touches schema-sensitive files; the attested plan allows
    # the deploy to proceed (the migrations remain individually gated and
    # the post-restart schema signature still fails closed).
    migration_guard(REPO, _fake_old_release(tmp_path), _TARGET)