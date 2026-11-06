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


def test_migration_guard_handles_missing_old_revision_with_attestation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("MNERYS_DEPLOY_MIGRATION_PLAN_APPROVED", "1")
    # A release revision that does not exist in this checkout is refused by
    # default and accepted only under the explicit operator attestation.
    ghost = tmp_path / "c3aa320804f1"
    ghost.mkdir()
    (ghost / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    migration_guard(REPO, ghost, _TARGET)


def test_update_source_commit_preserves_other_environment_lines():
    from scripts.deploy_native_preview import update_source_commit

    env = (
        "DATABASE_URL=postgresql+psycopg2://user@example.com/mnerys\n"
        "MNERYS_M21_WEB_CORE_CUTOVER=1\n"
        "ODYSSEUS_SOURCE_COMMIT=old\n"
    )
    updated = update_source_commit(env, "newsha123456")
    assert "ODYSSEUS_SOURCE_COMMIT=newsha123456" in updated
    assert "MNERYS_M21_WEB_CORE_CUTOVER=1" in updated
    # A hardcoded default never re-enables a deliberately disabled flag.
    assert updated.count("ODYSSEUS_SOURCE_COMMIT=") == 1


def test_effective_cutover_active_reads_the_live_process(tmp_path, monkeypatch):
    from scripts.deploy_native_preview import effective_cutover_active

    pid_dir = tmp_path / "proc" / "4321"
    pid_dir.mkdir(parents=True)
    (pid_dir / "environ").write_bytes(
        b"PATH=/usr/bin\0MNERYS_M21_WEB_CORE_CUTOVER=1\0SECRET=x\0",
    )
    monkeypatch.setattr(
        "scripts.deploy_native_preview.call",
        lambda *args, **kwargs: "MainPID=4321",
    )
    assert effective_cutover_active("mnerys-preview-app.service", proc_root=tmp_path / "proc") is True

    disabled = tmp_path / "proc" / "4322"
    disabled.mkdir(parents=True)
    (disabled / "environ").write_bytes(b"PATH=/usr/bin\0")
    monkeypatch.setattr(
        "scripts.deploy_native_preview.call",
        lambda *args, **kwargs: "MainPID=4322",
    )
    assert effective_cutover_active("mnerys-preview-app.service", proc_root=tmp_path / "proc") is False


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