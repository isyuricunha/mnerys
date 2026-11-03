#!/usr/bin/env python3
"""Validate and deploy the Mnerys native Tailscale preview with code rollback.

Use from a clean local checkout on the preview host. Never publishes private
runtime configuration. One application writer; a brief restart is expected.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib import error, request


SAFE_TESTS = (
    "tests/test_mnerys_branding.py",
    "tests/test_mnerys_visual_branding.py",
    "tests/test_native_preview_smoke.py",
    "tests/test_native_preview_deploy.py",
    "tests/test_nery_mascot_license.py",
    "tests/test_static_stylesheet_manifest.py",
)
SCHEMA_SENSITIVE = (
    "core/database.py",
    "core/models.py",
    "core/db_migrations.py",
    "migrations/",
    "alembic/",
)


class DeployError(RuntimeError):
    """A safe, non-secret-bearing explanation of a blocked operation."""


def call(args, *, cwd=None, env=None, timeout=150):
    """Execute a fixed argv, never a shell, and suppress private command output."""
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError("Required deployment operation did not complete") from exc
    if result.returncode:
        raise DeployError("Deployment command failed; inspect private host logs")
    return result.stdout


def private_write(path: Path, value: str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


@contextmanager
def exclusive_lock(root: Path):
    lockfile = root / "deployment.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    with open(lockfile, "a+", encoding="utf-8") as lock:
        os.chmod(lockfile, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("Another deployment is already in progress") from exc
        yield


def unit_release(contents: str) -> Path:
    matches = re.findall(r"(?m)^WorkingDirectory=(.+)$", contents)
    if len(matches) != 1:
        raise DeployError("Unexpected native systemd service layout")
    return Path(matches[0].strip())


def unit_python(contents: str) -> Path:
    matches = re.findall(r"(?m)^ExecStart=(\S+)\s+-m\s+uvicorn\b", contents)
    if len(matches) != 1:
        raise DeployError("Unsupported app command in native systemd service")
    return Path(matches[0])


def update_unit(contents: str, old: Path, new: Path, python: Path) -> str:
    old_python = unit_python(contents)
    if not old_python.exists() or not python.is_file():
        raise DeployError("Application Python runtime missing")
    updated = contents.replace(
        "WorkingDirectory=" + str(old), "WorkingDirectory=" + str(new), 1,
    )
    updated = updated.replace(
        "ExecStart=" + str(old_python) + " -m uvicorn",
        "ExecStart=" + str(python) + " -m uvicorn",
        1,
    )
    # MCP subprocesses and helper tools must also inherit the candidate venv.
    updated = updated.replace(
        "Environment=PATH=" + str(old_python.parent),
        "Environment=PATH=" + str(python.parent),
        1,
    )
    if updated == contents or str(old) == str(new):
        raise DeployError("Deployment would not change the selected release")
    return updated


def update_source_commit(contents: str, sha: str) -> str:
    matches = list(re.finditer(r"(?m)^ODYSSEUS_SOURCE_COMMIT=[^\r\n]*$", contents))
    if not matches:
        raise DeployError("Source revision setting missing in private environment")
    match = matches[-1]
    return contents[:match.start()] + "ODYSSEUS_SOURCE_COMMIT=" + sha + contents[match.end():]


def public_endpoint(private: Path) -> str:
    portfile = private / "preview.port"
    try:
        port = int(portfile.read_text().strip())
        cfg = (private / "Caddyfile").read_text()
    except (OSError, ValueError) as exc:
        raise DeployError("Private preview gateway configuration unavailable") from exc
    sites = re.findall(r"(?m)^http://([\d.]+):(\d+)\s*\{", cfg)
    if len(sites) != 1:
        raise DeployError("Unexpected gateway site layout")
    host, configured = sites[0]
    if configured != str(port) or not (49152 <= port <= 65535):
        raise DeployError("Fixed high-port configuration does not match gateway")
    if ipaddress.ip_address(host) not in ipaddress.ip_network((0x64400000, 10)):
        raise DeployError("Gateway is not bound to a Tailscale address")
    if "bind " + host not in cfg:
        raise DeployError("Gateway binding is not Tailscale-only")
    return f"http://{host}:{port}"


def repo_revision(repo: Path) -> str:
    if call(["git", "status", "--porcelain"], cwd=repo).strip():
        raise DeployError("Checkout is not clean; commit/review changes first")
    if call(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).strip() != "main":
        raise DeployError("Deploy only from the reviewed main branch")
    revision = call(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise DeployError("Invalid source revision")
    return revision


def migration_guard(repo: Path, old_release: Path, target: str):
    # The local source may contain migrations unknown to the previous runtime.
    current = old_release.name
    if not re.fullmatch(r"[a-f0-9]{12}", current):
        raise DeployError("Unable to compare old release for migration safety")
    try:
        changed = call(
            ["git", "diff", "--name-only", current, target, "--",
             "core", "migrations", "alembic"], cwd=repo,
        ).splitlines()
    except DeployError as exc:
        raise DeployError("Cannot compare DB-sensitive source changes") from exc
    if any(
        file in SCHEMA_SENSITIVE or
        file.startswith(("migrations/", "alembic/")) or
        ("migration" in file.lower() and file.startswith("core/"))
        for file in changed
    ):
        raise DeployError("Database-sensitive code changed: requires a reviewed migration/rollback plan")


def build_candidate(repo: Path, runtime: Path, revision: str) -> tuple[Path, Path]:
    releases = runtime / "releases"
    venvs = runtime / "venvs"
    releases.mkdir(parents=True, exist_ok=True)
    venvs.mkdir(parents=True, exist_ok=True)
    release = releases / revision[:12]
    venv = venvs / revision[:12]
    if not release.exists():
        temp = Path(tempfile.mkdtemp(prefix=".candidate-", dir=releases))
        try:
            archive = temp.parent / (temp.name + ".tar")
            try:
                with archive.open("wb") as out:
                    process = subprocess.run(
                        ["git", "archive", revision], cwd=repo,
                        stdout=out, stderr=subprocess.DEVNULL, timeout=45,
                    )
                if process.returncode:
                    raise DeployError("Unable to build candidate archive")
                with tarfile.open(archive) as tar:
                    tar.extractall(temp, filter="data")
                if not (temp / "requirements.txt").is_file():
                    raise DeployError("Candidate requirements are missing")
                temp.rename(release)
            finally:
                archive.unlink(missing_ok=True)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    if not (release / "requirements.txt").is_file():
        raise DeployError("Existing release is incomplete")
    if not (venv / "bin/python").exists():
        if venv.exists():
            shutil.rmtree(venv)
        uv = shutil.which("uv")
        if not uv:
            raise DeployError("uv package manager not installed")
        call([uv, "venv", "--python", "3.12", str(venv)], timeout=60)
        call(
            [uv, "pip", "install", "--python", str(venv / "bin/python"),
             "-r", str(release / "requirements.txt")],
            cwd=release, timeout=270,
        )
    return release, venv / "bin/python"


def validate_candidate(release: Path, python: Path):
    tests = [name for name in SAFE_TESTS if (release / name).is_file()]
    if len(tests) != len(SAFE_TESTS):
        raise DeployError("Candidate validation tests are incomplete")
    call([str(python), "-m", "pytest", "-q", *tests], cwd=release, timeout=150)
    node = shutil.which("node")
    if not node:
        raise DeployError("Node.js required for JS syntax checks")
    for name in ("static/js/theme.js", "static/js/models.js", "static/sw.js"):
        call([node, "--check", str(release / name)], cwd=release, timeout=25)


def sql_schema(data: Path) -> dict[str, str]:
    signatures = {}
    for path in sorted(data.rglob("*.db")):
        if path.is_symlink():
            raise DeployError("Symlinked database is not supported")
        relative = str(path.relative_to(data))
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=4)) as db:
                objects = db.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                ).fetchall()
                user_version = db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise DeployError("Persistent database is not readable") from exc
        contents = json.dumps([user_version, objects], sort_keys=True)
        signatures[relative] = hashlib.sha256(contents.encode()).hexdigest()
    return signatures


def consistent_backup(data: Path, backups: Path, revision: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backups / (timestamp + "-" + revision[:12])
    if destination.exists():
        raise DeployError("A backup with this name already exists")
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    # Only relative in-tree symlinks are accepted (HF model cache uses these).
    for item in data.rglob("*"):
        if item.is_symlink() and (
            item.readlink().is_absolute() or not item.resolve().is_relative_to(data.resolve())
        ):
            raise DeployError("Unsafe symlink in persistent data")
    def exclude(_directory, names):
        return {n for n in names if n.endswith(("-wal", "-shm", "-journal"))}
    shutil.copytree(data, destination / "data", symlinks=True, ignore=exclude)
    for original in sorted(data.rglob("*.db")):
        target = destination / "data" / original.relative_to(data)
        temporary = target.with_name(target.name + ".backup")
        with closing(sqlite3.connect(f"file:{original}?mode=ro", uri=True, timeout=6)) as source:
            with closing(sqlite3.connect(temporary, timeout=6)) as copy:
                source.backup(copy, pages=64, sleep=0.05)
        temporary.replace(target)
    for folder, dirs, files in os.walk(destination, followlinks=False):
        os.chmod(folder, 0o700)
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink():
                os.chmod(path, 0o600)
    completed = destination / "BACKUP_COMPLETE"
    completed.write_text("SQLite online backup completed\n")
    completed.chmod(0o600)
    return destination


def checks(base: str, revision: str) -> None:
    import urllib.parse
    def fetch(path: str) -> tuple[int, bytes]:
        try:
            with request.urlopen(base + path, timeout=3) as response:
                return response.status, response.read(150_000)
        except error.HTTPError as exc:
            return exc.code, exc.read(2048)

    status, body = fetch("/api/health")
    if status != 200 or json.loads(body).get("status") != "healthy":
        raise DeployError("Application liveness failed")
    status, body = fetch("/login")
    if status != 200 or b"Mnerys" not in body:
        raise DeployError("Application login page failed")
    status, _ = fetch("/api/ready")
    if status not in (401, 403):
        raise DeployError("Readiness authentication guard failed")
    status, body = fetch("/api/version")
    if status != 200 or revision not in body.decode("utf-8", errors="replace"):
        raise DeployError("Live application is not the expected release")


def poll_live(base: str, revision: str, seconds: float = 55):
    until = time.monotonic() + seconds
    while True:
        try:
            checks(base, revision)
            return
        except (DeployError, OSError, ValueError, TimeoutError):
            if time.monotonic() >= until:
                raise DeployError("New version did not pass post-deploy checks")
            time.sleep(0.7)


def trusted_readiness(release: Path, python: Path, envfile: Path):
    command = (
        "import sys; from dotenv import load_dotenv; "
        "load_dotenv(sys.argv[1], override=True); "
        "from src.readiness import check_readiness; "
        "sys.exit(0 if check_readiness()['ready'] else 4)"
    )
    call([str(python), "-c", command, str(envfile)], cwd=release, timeout=35)


def unit_and_config(home: Path):
    private = home / ".config/mnerys-preview"
    selection = private / "app-unit"
    if not selection.is_file() or selection.stat().st_mode & 0o077:
        raise DeployError("Private systemd unit selection unavailable or insecure")
    service_name = selection.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service_name):
        raise DeployError("Invalid private systemd unit name")
    unit = home / ".config/systemd/user" / service_name
    envfile = private / "runtime.env"
    if not unit.is_file() or not envfile.is_file():
        raise DeployError("Private native service configuration missing")
    if envfile.stat().st_mode & 0o077:
        raise DeployError("Private runtime settings are not protected")
    return unit, envfile, private, service_name


def require_sqlite_backup_policy(environment: str) -> None:
    """This deployer snapshots SQLite; refuse a PostgreSQL cutover without pg_dump."""
    entries = [
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in environment.splitlines()
        if line.startswith("DATABASE_URL=")
    ]
    if entries and not entries[-1].lower().startswith(("sqlite://", "sqlite+pysqlite://")):
        raise DeployError(
            "Native preview rollback requires a reviewed PostgreSQL backup "
            "strategy before DATABASE_URL is switched"
        )


def deploy(repo: Path, home: Path, dry_run: bool = False) -> dict:
    runtime = home / ".local/share/mnerys-preview"
    unit, envfile, private, service_name = unit_and_config(home)
    with exclusive_lock(runtime):
        base = public_endpoint(private)
        revision = repo_revision(repo)
        before_unit = unit.read_text()
        before_env = envfile.read_text()
        require_sqlite_backup_policy(before_env)
        prior = unit_release(before_unit)
        if not prior.is_dir() or not prior.is_relative_to(runtime / "releases"):
            raise DeployError("Current service is not using a versioned release")
        previous_revision = prior.name
        if revision.startswith(previous_revision):
            checks(base, revision)
            return {"result": "unchanged", "revision": revision[:12]}
        migration_guard(repo, prior, revision)
        candidate, python = build_candidate(repo, runtime, revision)
        validate_candidate(candidate, python)
        if dry_run:
            return {"result": "validated", "revision": revision[:12], "promoted": False}

        data = runtime / "data"
        if not data.is_dir() or not (data / "auth.json").is_file():
            raise DeployError("Persistent state or authentication config missing")
        schema_before = sql_schema(data)
        backup = consistent_backup(data, runtime / "backups", previous_revision)
        before_python = unit_python(before_unit)
        newunit = update_unit(before_unit, prior, candidate, python)
        newenv = update_source_commit(before_env, revision)

        # Restart once. Roll back CODE + service config on failure only if
        # database schema remained compatible. Never blindly restore state.
        try:
            private_write(unit, newunit)
            private_write(envfile, newenv)
            call(["systemctl", "--user", "daemon-reload"], timeout=20)
            call(["systemctl", "--user", "restart", service_name], timeout=75)
            poll_live(base, revision)
            trusted_readiness(candidate, python, envfile)
            if sql_schema(data) != schema_before:
                raise DeployError("Unexpected database schema change after deployment")
        except BaseException as error:
            if sql_schema(data) != schema_before:
                subprocess.run(
                    ["systemctl", "--user", "stop", service_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                )
                raise DeployError(
                    "Database schema changed: service stopped; manual recovery required, "
                    "data and backup preserved"
                ) from error
            private_write(unit, before_unit)
            private_write(envfile, before_env)
            call(["systemctl", "--user", "daemon-reload"], timeout=20)
            call(["systemctl", "--user", "restart", service_name], timeout=75)
            poll_live(base, previous_revision)
            trusted_readiness(prior, before_python, envfile)
            raise DeployError(
                "Candidate failed validation; previous healthy release restored"
            ) from error

        return {
            "result": "deployed",
            "revision": revision[:12],
            "previous": previous_revision,
            "backup": "created privately",
            "liveness": "passed",
            "readiness": "passed",
            "login": "passed",
            "auth": "enforced",
            "gateway": "unchanged",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe native Mnerys preview deployment")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Stage and test without service restart")
    group.add_argument("--deploy", action="store_true", help="Deploy and automatically roll back code on validation failure")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        report = deploy(repo, Path.home(), dry_run=args.check)
        print(json.dumps(report, indent=2))
    except DeployError as exc:
        print("DEPLOY FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
