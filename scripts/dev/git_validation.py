#!/usr/bin/env python3
"""Native Git hook validation for this repository, without hook managers.

Pre-commit validates staged blobs (not potentially different working-tree
copies). Comprehensive CI-like validation is opt-in via the explicit `full`
command, not a Git push hook. Hosted GitHub CI still runs independently.
"""

from __future__ import annotations

import os
import argparse
import json
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


def staged_js_syntax_command(path: str) -> list[str]:
    """Parse staged browser/Playwright ES modules using their actual syntax.

    Node treats stdin independently of the file's on-disk module context.
    Other legacy .js files retain their previous CommonJS syntax check.
    """
    if path.endswith(".mjs") or path.startswith((
        "frontend/src/", "static/js/", "tests/e2e/gateway-notes/",
    )):
        return ["node", "--check", "--input-type=module", "-"]
    return ["node", "--check", "-"]


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
            run(staged_js_syntax_command(path), input_bytes=blob)
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


def validate_full() -> None:
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


def affected_plan(*, base: str | None = None, paths: list[str] | None = None) -> dict:
    """Analyze tracked changes without importing the app or collecting pytest."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.dev.affected_tests import load_contracts, select_tests

    changed = set(paths or ())
    if paths is None:
        revisions = ["--no-renames", "--name-only", "-z", "HEAD"]
        changed.update(os.fsdecode(p) for p in git_capture("diff", *revisions).split(b"\0") if p)
        if base:
            changed.update(os.fsdecode(p) for p in git_capture(
                "diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD",
            ).split(b"\0") if p)
    for path in changed:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError("Changed paths must be repository-relative")
    tracked = [os.fsdecode(p) for p in git_capture("ls-files", "-z").split(b"\0") if p]
    return select_tests(ROOT, sorted(changed), tracked, load_contracts(ROOT / "scripts/dev/test_contracts.json"))


def validate_affected(*, base=None, paths=None, dry_run=False, as_json=False) -> None:
    plan = affected_plan(base=base, paths=paths)
    if as_json:
        print(json.dumps(plan, indent=2))
    else:
        print(f"[git-validation] Affected: {len(plan['python_tests'])} Python / {len(plan['node_tests'])} Node test files.")
        if plan["persistence"]:
            print("[git-validation] Isolated persistence contracts: " + ", ".join(plan["persistence"]))
    if dry_run:
        return
    if plan["unmapped"] or plan["full_required"]:
        raise ValidationError(
            "Selection requires review; add contract coverage for unmapped changes "
            "or explicitly use full validation for shared test infrastructure: "
            + ", ".join(plan["unmapped"] + plan["full_required"])
        )
    run(["git", "diff", "HEAD", "--check"])
    for path in plan["changed"]:
        file = ROOT / path
        if not file.is_file():
            continue
        if path.endswith(".py"):
            try:
                compile(file.read_bytes(), path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError) as exc:
                raise ValidationError(f"Changed Python syntax failed: {path}: {exc}") from exc
        elif path.endswith((".js", ".mjs", ".cjs")):
            run(staged_js_syntax_command(path), input_bytes=file.read_bytes())
    if plan["python_tests"]:
        run([python_for_tests(), "-m", "pytest", "-q", *plan["python_tests"]])
    if plan["node_tests"]:
        run(["node", "--test", *plan["node_tests"]])
    if not plan["python_tests"] and not plan["node_tests"]:
        print("[git-validation] Static/documentation-only changes; no test process started.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="affected", choices=("affected", "pre-commit", "full"))
    parser.add_argument("--base", help="Also include committed changes since this merge base")
    parser.add_argument("--paths", nargs="+", help="Explicit repository-relative changed paths")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; never collect or execute tests")
    parser.add_argument("--json", action="store_true", help="Explain each selected test and required CI/DB contracts")
    options = parser.parse_args()
    try:
        if options.command != "affected" and any((options.base, options.paths, options.dry_run, options.json)):
            raise ValidationError("Selection options apply only to the affected command")
        if options.command == "pre-commit":
            validate_staged()
        elif options.command == "full":
            validate_full()
        else:
            validate_affected(base=options.base, paths=options.paths, dry_run=options.dry_run, as_json=options.json)
    except ValidationError as exc:
        print(f"[git-validation] BLOCKED: {exc}", file=sys.stderr, flush=True)
        return 1
    except ValueError as exc:
        print(f"[git-validation] BLOCKED: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
