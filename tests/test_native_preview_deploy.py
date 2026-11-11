"""Unit and integration tests for the native deploy validation/rollback policy."""
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "native_deployer", ROOT / "scripts/deploy_native_preview.py"
)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_unit_promotion_updates_release_and_venv(tmp_path):
    old = tmp_path / "releases/111111111111"
    new = tmp_path / "releases/222222222222"
    old_python = tmp_path / "venv/bin/python"
    new_python = tmp_path / "venvs/222222222222/bin/python"
    for file in (old_python, new_python):
        file.parent.mkdir(parents=True)
        file.write_text("test")
    unit = (
        f"WorkingDirectory={old}\n"
        f"Environment=PATH={old_python.parent}:/usr/bin\n"
        f"ExecStart={old_python} -m uvicorn app:app --host localhost\n"
    )
    updated = deployment.update_unit(unit, old, new, new_python)
    assert f"WorkingDirectory={new}" in updated
    assert f"ExecStart={new_python} -m uvicorn" in updated
    assert f"Environment=PATH={new_python.parent}:/usr/bin" in updated
    assert "ExecStart=" + str(old_python) not in updated


def test_native_release_metadata_from_exact_semver_tag(monkeypatch, tmp_path):
    revision = "e" * 40

    def fake_call(args, **_kwargs):
        assert args == ["git", "tag", "--points-at", revision]
        return "v0.19.0\n"

    monkeypatch.setattr(deployment, "call", fake_call)
    assert deployment.revision_release_version(tmp_path, revision) == "0.19.0"

    env = "AUTH_ENABLED=true\nODYSSEUS_SOURCE_COMMIT=old\n"
    updated = deployment.update_release_version(env, "0.19.0")
    assert updated.endswith("MNERYS_RELEASE_VERSION=0.19.0\n")
    assert updated.count("MNERYS_RELEASE_VERSION=") == 1
    assert deployment.update_release_version(updated, "0.19.1").endswith(
        "MNERYS_RELEASE_VERSION=0.19.1\n"
    )
    assert deployment.update_release_version(updated, "0.19.1").count(
        "MNERYS_RELEASE_VERSION="
    ) == 1


def test_native_untagged_version_is_honest_and_invalid_is_refused(monkeypatch, tmp_path):
    revision = "b" * 40
    monkeypatch.setattr(deployment, "call", lambda *_a, **_k: "")
    value = deployment.revision_release_version(tmp_path, revision)
    assert value == "0.0.0-dev." + revision[:12]
    assert "MNERYS_RELEASE_VERSION=" + value in deployment.update_release_version("", value)
    with pytest.raises(deployment.DeployError, match="Invalid release version"):
        deployment.update_release_version("", "0.19.0\nAUTH_ENABLED=false")


def test_explicit_stable_tag_requires_exact_ancestor(monkeypatch, tmp_path):
    current = "c" * 40
    target = "b" * 40
    calls = []

    def fake_call(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return target + "\n"
        if argv[:2] == ["git", "tag"]:
            return "v0.19.0\n"
        if argv[:2] == ["git", "merge-base"]:
            return ""
        raise AssertionError(argv)

    monkeypatch.setattr(deployment, "call", fake_call)
    assert deployment.reviewed_stable_revision(tmp_path, current, "v0.19.0") == target
    assert ["git", "merge-base", "--is-ancestor", target, current] in calls
    with pytest.raises(deployment.DeployError, match="exact stable"):
        deployment.reviewed_stable_revision(tmp_path, current, "HEAD")


def test_explicit_tag_rejects_nonancestor_and_mismatch(monkeypatch, tmp_path):
    current = "c" * 40
    target = "b" * 40

    def nonancestor(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return target
        raise deployment.DeployError("not a parent")

    monkeypatch.setattr(deployment, "call", nonancestor)
    with pytest.raises(deployment.DeployError, match="not in the reviewed"):
        deployment.reviewed_stable_revision(tmp_path, current, "v0.19.0")

    def wrongtag(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return target
        if argv[:2] == ["git", "tag"]:
            return "v0.18.0"
        return ""

    monkeypatch.setattr(deployment, "call", wrongtag)
    with pytest.raises(deployment.DeployError, match="identity"):
        deployment.reviewed_stable_revision(tmp_path, current, "v0.19.0")


def test_update_commit_changes_only_last_setting():
    original = "ODYSSEUS_SOURCE_COMMIT=old\nAUTH_ENABLED=true\nODYSSEUS_SOURCE_COMMIT=old2\n"
    changed = deployment.update_source_commit(original, "a" * 40)
    assert changed == (
        "ODYSSEUS_SOURCE_COMMIT=old\nAUTH_ENABLED=true\n"
        "ODYSSEUS_SOURCE_COMMIT=" + "a" * 40 + "\n"
    )


def test_public_gateway_only_accepts_tailnet_bindings(tmp_path):
    # Generate fixture address rather than publishing an actual private address.
    import ipaddress
    address = str(ipaddress.ip_address(0x6440001F))
    config = tmp_path / "preview.port"
    config.write_text("53921\n")
    (tmp_path / "Caddyfile").write_text(
        f"http://{address}:53921 {{\n    bind {address}\n}}\n"
    )
    assert deployment.public_endpoint(tmp_path).endswith(":53921")
    (tmp_path / "Caddyfile").write_text(
        f"http://{address}:53921 {{\n}}\n"
    )
    with pytest.raises(deployment.DeployError, match="binding"):
        deployment.public_endpoint(tmp_path)


def test_consistent_backup_preserves_sqlite_rows_files_and_relative_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "app.db") as conn:
        conn.execute("CREATE TABLE conversation (id INTEGER PRIMARY KEY, text TEXT)")
        conn.execute("INSERT INTO conversation (text) VALUES ('example')")
    (data / "auth.json").write_text('{"demo":true}')
    blob = data / "blob"
    blob.mkdir()
    (blob / "target.bin").write_bytes(b"abc")
    (blob / "link.bin").symlink_to("target.bin")
    with sqlite3.connect(data / "app.db") as conn:
        before = deployment.sql_schema(data)
        snapshot = deployment.consistent_backup(data, tmp_path / "backups", "1" * 12)
        assert (snapshot / "BACKUP_COMPLETE").is_file()
        assert (snapshot / "data/auth.json").read_text() == '{"demo":true}'
        assert (snapshot / "data/blob/link.bin").is_symlink()
        assert (snapshot / "data/blob/link.bin").read_bytes() == b"abc"
        with sqlite3.connect(snapshot / "data/app.db") as saved:
            assert saved.execute("SELECT text FROM conversation").fetchone() == ("example",)
        assert deployment.sql_schema(snapshot / "data") == before
    assert not (snapshot / "data/app.db-wal").exists()


def test_backup_refuses_escaping_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "escaped").symlink_to(tmp_path.parent)
    with pytest.raises(deployment.DeployError, match="symlink"):
        deployment.consistent_backup(data, tmp_path / "backups", "2" * 12)


def dummy_runtime(tmp_path, monkeypatch, *, schema_drift=False):
    """Create a fully isolated deployment using mocks, never calling systemd."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = home / ".local/share/mnerys-preview"
    old = runtime / "releases" / ("1" * 12)
    new = runtime / "releases" / ("2" * 12)
    for f in (old / "static/index.html", new / "static/index.html"):
        f.parent.mkdir(parents=True)
        f.write_text("Mnerys")
    old_python = runtime / "venv/bin/python"
    new_python = runtime / "venvs" / ("2" * 12) / "bin/python"
    for f in (old_python, new_python):
        f.parent.mkdir(parents=True)
        f.write_text("dummy")
    data = runtime / "data"
    data.mkdir()
    (data / "auth.json").write_text("{}")
    (data / "app.db").write_text("test data mock")
    service_name = "example-preview.service"
    selection = home / ".config/mnerys-preview/app-unit"
    selection.parent.mkdir(parents=True)
    selection.write_text(service_name + "\n")
    selection.chmod(0o600)
    service_file = home / ".config/systemd/user" / service_name
    service_file.parent.mkdir(parents=True)
    original_unit = (
        f"WorkingDirectory={old}\n"
        f"Environment=PATH={old_python.parent}:/usr/bin\n"
        f"ExecStart={old_python} -m uvicorn app:app\n"
    )
    service_file.write_text(original_unit)
    envfile = home / ".config/mnerys-preview/runtime.env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("AUTH_ENABLED=true\nODYSSEUS_SOURCE_COMMIT=" + "1"*40 + "\n")
    envfile.chmod(0o600)
    monkeypatch.setattr(deployment, "public_endpoint", lambda private: "http://example.com:53921")
    monkeypatch.setattr(deployment, "repo_revision", lambda repo: "2"*40)
    monkeypatch.setattr(deployment, "migration_guard", lambda *args: None)
    monkeypatch.setattr(deployment, "build_candidate", lambda *args: (new, new_python))
    monkeypatch.setattr(deployment, "validate_candidate", lambda *args: None)
    monkeypatch.setattr(deployment, "consistent_backup", lambda *args: tmp_path / "fake_backup")
    calls = []
    monkeypatch.setattr(deployment, "call", lambda args, **kwargs: calls.append(tuple(args)) or "")
    schema_calls = []
    def schema(_data):
        schema_calls.append(1)
        return {"app.db": "new"} if schema_drift and len(schema_calls)>1 else {"app.db": "old"}
    monkeypatch.setattr(deployment, "sql_schema", schema)
    return home, repo, service_file, envfile, original_unit, calls


def test_validation_only_does_not_restart_or_change_private_config(tmp_path, monkeypatch):
    home, repo, unit, env, oldunit, calls = dummy_runtime(tmp_path, monkeypatch)
    result = deployment.deploy(repo, home, dry_run=True)
    assert result["result"] == "validated"
    assert unit.read_text() == oldunit
    assert not calls


def test_failed_candidate_restores_old_service_and_env(tmp_path, monkeypatch):
    home, repo, unit, env, oldunit, calls = dummy_runtime(tmp_path, monkeypatch)
    original_env = env.read_text()
    revisions = []
    def poll(_base, rev):
        revisions.append(rev)
        if rev.startswith("2"):
            raise deployment.DeployError("simulated unhealthy candidate")
    monkeypatch.setattr(deployment, "poll_live", poll)
    monkeypatch.setattr(deployment, "trusted_readiness", lambda *args: None)
    with pytest.raises(deployment.DeployError, match="previous healthy release restored"):
        deployment.deploy(repo, home)
    assert unit.read_text() == oldunit
    assert env.read_text() == original_env
    assert sum(cmd[2] == "restart" for cmd in calls if len(cmd)>2 and cmd[0]=="systemctl") == 2
    assert revisions == ["2"*40, "1"*12]


def test_schema_drift_stops_service_and_preserves_migrated_state(tmp_path, monkeypatch):
    home, repo, unit, env, oldunit, calls = dummy_runtime(tmp_path, monkeypatch, schema_drift=True)
    monkeypatch.setattr(
        deployment, "poll_live",
        lambda *args: (_ for _ in ()).throw(deployment.DeployError("simulated failure")),
    )
    monkeypatch.setattr(deployment, "trusted_readiness", lambda *args: None)
    stopped = []
    monkeypatch.setattr(
        deployment.subprocess, "run",
        lambda argv, **kwargs: stopped.append(tuple(argv)) or SimpleNamespace(returncode=0),
    )
    with pytest.raises(deployment.DeployError, match="manual recovery required"):
        deployment.deploy(repo, home)
    assert "2"*12 in unit.read_text()
    assert any(cmd[2] == "stop" for cmd in stopped)


def test_preview_deployer_refuses_postgresql_without_backup_plan():
    from scripts.deploy_native_preview import DeployError, require_sqlite_backup_policy
    require_sqlite_backup_policy("AUTH_ENABLED=true\n")
    require_sqlite_backup_policy("DATABASE_URL=sqlite:///data/app.db\n")
    with pytest.raises(DeployError, match="PostgreSQL backup strategy"):
        require_sqlite_backup_policy(
            "DATABASE_URL=postgresql+psycopg2://test:<REDACTED>@db.example.com/mnerys\n"
        )


def test_policy_docs_distinguish_implemented_from_planned():
    docs = (ROOT / "docs/deployment/NATIVE_PREVIEW.md").read_text()
    assert "deploy with validation and rollback" in docs.lower()
    assert "scripts/deploy_native_preview.py" in docs
    assert "SQLite" in docs
    assert "<TAILSCALE_HOST>" in docs



def test_postgres_backup_requires_private_executable(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(deployment.DeployError, match="unavailable"):
        deployment.postgres_backup_helper(private)
    helper = tmp_path / "backup-helper"
    helper.write_text("#!/usr/bin/env python3\n")
    helper.chmod(0o700)
    selection = private / "postgres-backup-helper"
    selection.write_text(str(helper) + "\n")
    selection.chmod(0o600)
    assert deployment.postgres_backup_helper(private) == helper
    selection.chmod(0o644)
    with pytest.raises(deployment.DeployError, match="unavailable"):
        deployment.postgres_backup_helper(private)


def test_postgresql_native_deploy_backs_up_and_validates(tmp_path, monkeypatch):
    home, repo, unit, env, oldunit, calls = dummy_runtime(tmp_path, monkeypatch)
    env.write_text(env.read_text() + "DATABASE_URL=postgresql+psycopg2://tester:<REDACTED>@db.example.com/mnerys\n")
    env.chmod(0o600)
    private = home / ".config/mnerys-preview"
    helper = tmp_path / "backup-helper"
    helper.write_text("#!/usr/bin/env python3\n")
    helper.chmod(0o700)
    (private / "postgres-backup-helper").write_text(str(helper) + "\n")
    (private / "postgres-backup-helper").chmod(0o600)
    events = []
    monkeypatch.setattr(deployment, "postgres_schema_signature", lambda *args: "stable")
    monkeypatch.setattr(
        deployment, "verified_postgresql_backup",
        lambda private, directory: events.append("postgres_backup"),
    )
    monkeypatch.setattr(deployment, "poll_live", lambda *args: events.append("health"))
    monkeypatch.setattr(deployment, "trusted_readiness", lambda *args: events.append("ready"))
    result = deployment.deploy(repo, home)
    assert result["result"] == "deployed"
    assert "postgres_backup" in events
    assert events.index("postgres_backup") < events.index("health")


def test_postgres_backend_change_not_silently_ignored():
    assert deployment.selected_database_backend("AUTH_ENABLED=true\n") == "sqlite"
    assert deployment.selected_database_backend(
        "DATABASE_URL=postgresql+psycopg2://tester:<REDACTED>@db.example.com/mnerys\n"
    ) == "postgresql"
    with pytest.raises(deployment.DeployError, match="Unsupported"):
        deployment.selected_database_backend("DATABASE_URL=mysql://example.com/test\n")
