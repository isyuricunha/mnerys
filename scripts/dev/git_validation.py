#!/usr/bin/env python3
"""Native Git hook validation for this repository, without hook managers.

Pre-commit validates staged blobs (not potentially different working-tree
copies). Pre-push repeats the repository's blocking, locally runnable CI
syntax/architecture tests and all four pytest shards. CI-only browser,
sandbox setup and hosted security jobs remain independently authoritative.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SHARDS = ("1/4", "2/4", "3/4", "4/4")


class ValidationError(Exception):
    """A blocking local validation failure."""


def run(command: list[str], *, input_bytes: bytes | None = None) -> bytes:
    print("[git-validation] " + " ".join(command[:4]), flush=True)
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            cwd=ROOT,
            stdout=subprocess.PIPE if input_bytes is not None else None,
            stderr=subprocess.PIPE if input_bytes is not None else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValidationError(f"Required executable is not installed: {command[0]}") from exc
    if result.returncode:
        if input_bytes is not None and result.stderr:
            sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        raise ValidationError(
            f"Check failed (exit {result.returncode}): {' '.join(command[:4])}"
        )
    return result.stdout or b""


def git_capture(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=ROOT,
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ValidationError(f"Git inspection failed: {args[0]}")
    return result.stdout


def staged_paths() -> list[str]:
    data = git_capture(
        "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z",
    )
    return [os.fsdecode(value) for value in data.split(b"\0") if value]


def staged_blob(path: str) -> bytes:
    return git_capture("show", f":{path}")


def validate_staged() -> None:
    run(["git", "diff", "--cached", "--check"])
    paths = staged_paths()
    if not paths:
        print("[git-validation] No staged files to inspect.")
        return

    for path in paths:
        blob = staged_blob(path)
        if path.endswith(".py"):
            try:
                compile(blob, path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError) as exc:
                raise ValidationError(f"Staged Python syntax failed: {path}: {exc}") from exc
        elif path.endswith((".js", ".mjs", ".cjs")):
            options = (
                ["node", "--check", "--input-type=module", "-"]
                if path.endswith(".mjs") or path.startswith("frontend/src/")
                else ["node", "--check", "-"]
            )
            run(options, input_bytes=blob)
        elif (
            path.startswith(".githooks/")
            or path.endswith(".sh")
        ):
            interpreter = "sh" if path.startswith(".githooks/") else "bash"
            run([interpreter, "-n", "-"], input_bytes=blob)

    # Keep generated frontend modules checked against the *existing* build
    # only if relevant sources or the builder itself have changed.
    frontend_changed = any(
        path.startswith(("frontend/", "static/", "website/"))
        or path == "scripts/build_frontend.py"
        for path in paths
    )
    if frontend_changed:
        run([sys.executable, "scripts/build_frontend.py", "--check"])
    print(f"[git-validation] Staged syntax/whitespace passed ({len(paths)} files).")


def python_for_tests() -> str:
    configured = subprocess.run(
        ["git", "config", "--local", "--get", "mnerys.hooksPython"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    candidates: list[Path] = []
    if configured.returncode == 0 and configured.stdout.strip():
        configured_path = Path(configured.stdout.strip()).expanduser()
        if not configured_path.is_absolute():
            configured_path = ROOT / configured_path
        candidates.append(configured_path)
    else:
        candidates.extend([
            ROOT / "venv/bin/python",
            ROOT / ".venv/bin/python",
            Path(sys.executable),
        ])

    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [str(candidate), "-c", "import pytest"],
            cwd=ROOT, capture_output=True, check=False,
        )
        if result.returncode == 0:
            return str(candidate)

    raise ValidationError(
        "A working pytest environment is required. Set the repository-local "
        "'mnerys.hooksPython' to your existing project venv's Python, "
        "or create an appropriate project environment first."
    )


def validation_commands(python: str) -> list[list[str]]:
    """Blocking local approximation of the main CI Python/Node jobs."""
    checks: list[list[str]] = [
        [python, "-m", "compileall", "-q", "app.py", "core",
         "routes", "src", "services", "scripts", "tests"],
        [python, "scripts/build_frontend.py", "--check"],
        ["node", "scripts/check_frontend.mjs"],
    ]
    # Match the maintained source syntax glob from .github/workflows/ci.yml.
    js_files = sorted({
        *([ROOT / "static/app.js"] if (ROOT / "static/app.js").exists() else []),
        *(ROOT / "static/js").rglob("*.js"),
        *(ROOT / "frontend/src").rglob("*.js"),
        ROOT / "scripts/check_frontend.mjs",
    })
    checks.extend(["node", "--check", str(path.relative_to(ROOT))] for path in js_files)
    checks.extend([
        ["node", "--test", "tests/frontendArchitecture.test.mjs"],
        ["node", "--test", "tests/release/semantic_release.test.mjs"],
    ])
    checks.extend(
        [python, "-m", "pytest", "-q", "-rs", "--durations=25", "--shard", shard]
        for shard in SHARDS
    )
    return checks


def validate_push() -> None:
    if git_capture("status", "--porcelain").strip():
        raise ValidationError(
            "Working tree is not clean. Commit or remove untracked changes "
            "before validating the exact code that will be pushed."
        )
    python = python_for_tests()
    if shutil.which("node") is None:
        raise ValidationError("Node is required for the existing frontend CI gates.")

    print("[git-validation] Running locally available CI jobs; no Docker, installs or network.", flush=True)
    failed: list[str] = []
    for command in validation_commands(python):
        try:
            run(command)
        except ValidationError as exc:
            print(f"[git-validation] FAIL: {exc}", file=sys.stderr, flush=True)
            failed.append(" ".join(command[:4]))
    if failed:
        raise ValidationError(
            f"{len(failed)} local CI check(s) failed. Push blocked; fix and rerun."
        )
    print(
        "[git-validation] All four pytest shards and Python/Node checks passed. "
        "Hosted security, browser setup and container jobs still run in GitHub CI.",
        flush=True,
    )


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"pre-commit", "pre-push"}:
        print("Usage: git_validation.py [pre-commit|pre-push]", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "pre-commit":
            validate_staged()
        else:
            validate_push()
    except ValidationError as exc:
        print(f"[git-validation] BLOCKED: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
