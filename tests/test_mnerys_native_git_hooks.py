"""Behavioral acceptance for Mnerys' native Git hooks.

All Git mutations happen in disposable tmp_path repositories, never the
developer's real repository. No network, Docker or Git push is invoked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = SOURCE_ROOT / "scripts/dev/git_validation.py"
HOOKS = SOURCE_ROOT / ".githooks"


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "mnerys_git_hook_validator", VALIDATOR,
    )
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, capture_output=True, check=False, timeout=15,
    )


def fresh_repo(repo: Path) -> Path:
    repo.mkdir()
    result = git(repo, "init", "-q")
    assert result.returncode == 0, result.stderr
    return repo


def test_opt_in_full_commands_reuse_ci_shards_and_node_contract(module):
    commands = module.validation_commands("project-python")
    pytest_commands = [
        cmd for cmd in commands if cmd[:3] ==
        ["project-python", "-m", "pytest"]
    ]
    assert [cmd[-1] for cmd in pytest_commands] == [
        "1/4", "2/4", "3/4", "4/4",
    ]
    assert all(
        cmd[3:-1] == ["-q", "-rs", "--durations=25", "--shard"]
        for cmd in pytest_commands
    )
    assert ["node", "scripts/check_frontend.mjs"] in commands
    assert ["node", "--test", "tests/frontendArchitecture.test.mjs"] in commands
    assert ["node", "--test", "tests/release/semantic_release.test.mjs"] in commands
    assert any(cmd[1:3] == ["-m", "compileall"] for cmd in commands)
    assert not any("docker" in " ".join(cmd).lower() for cmd in commands)
    assert not any("--no-verify" in cmd for cmd in commands)


def test_staged_compiler_checks_index_not_unstaged_worktree(
    module, monkeypatch, tmp_path,
):
    repo = fresh_repo(tmp_path / "repo")
    monkeypatch.setattr(module, "ROOT", repo)
    source = repo / "module.py"
    source.write_text("def valid():\n    return 42\n")
    assert git(repo, "add", "module.py").returncode == 0
    source.write_text("def invalid(\n")
    module.validate_staged()  # A valid staged blob, despite broken worktree.

    # Reverse: the working tree is now fixed, but the staged content is not.
    assert git(repo, "add", "module.py").returncode == 0
    source.write_text("def corrected():\n    return 7\n")
    with pytest.raises(module.ValidationError, match="Staged Python syntax failed"):
        module.validate_staged()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_staged_javascript_syntax_blocks_invalid_index(
    module, monkeypatch, tmp_path,
):
    repo = fresh_repo(tmp_path / "repo")
    monkeypatch.setattr(module, "ROOT", repo)
    source = repo / "broken.mjs"
    source.write_text("export const valid = 1;\n")
    assert git(repo, "add", "broken.mjs").returncode == 0
    module.validate_staged()
    source.write_text("export const broken = ;\n")
    assert git(repo, "add", "broken.mjs").returncode == 0
    with pytest.raises(module.ValidationError, match="node"):
        module.validate_staged()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
@pytest.mark.parametrize("path", [
    "static/js/chat.js",
    "tests/e2e/gateway-notes/browser-ui.spec.js",
    "frontend/src/main.js",
    "example.mjs",
])
def test_staged_browser_es_modules_use_explicit_node_module_parser(module, path):
    command = module.staged_js_syntax_command(path)
    assert command == ["node", "--check", "--input-type=module", "-"]
    module.run(
        command, input_bytes=b"import { readFile } from 'node:fs';\nexport { readFile };\n",
    )
    with pytest.raises(module.ValidationError, match="node"):
        module.run(command, input_bytes=b"export const invalid = ;\n")


def test_legacy_staged_commonjs_keeps_existing_node_syntax_check(module):
    assert module.staged_js_syntax_command("legacy/fixture.js") == [
        "node", "--check", "-",
    ]


def test_opt_in_full_validation_refuses_dirty_tree_before_tests(
    module, monkeypatch, tmp_path,
):
    repo = fresh_repo(tmp_path / "repo")
    monkeypatch.setattr(module, "ROOT", repo)
    (repo / "private-work.py").write_text("print('work in progress')\n")
    with pytest.raises(module.ValidationError, match="not clean"):
        module.validate_full()


def test_exact_native_hook_prevents_invalid_git_commit(tmp_path):
    repo = fresh_repo(tmp_path / "repo")
    (repo / ".githooks").mkdir()
    (repo / "scripts/dev").mkdir(parents=True)
    shutil.copy2(VALIDATOR, repo / "scripts/dev/git_validation.py")
    for name in ("pre-commit",):
        destination = repo / ".githooks" / name
        shutil.copy2(HOOKS / name, destination)
        destination.chmod(0o755)
    assert git(repo, "config", "--local", "core.hooksPath", ".githooks").returncode == 0

    source = repo / "broken.py"
    source.write_text("def broken(\n")
    assert git(repo, "add", ".githooks", "scripts/dev/git_validation.py", "broken.py").returncode == 0
    bad_commit = git(
        repo, "-c", "user.name=Fixture Author",
        "-c", "user.email=fixture@example.com",
        "commit", "-m", "test: reject invalid Python syntax",
    )
    assert bad_commit.returncode != 0
    assert "Staged Python syntax failed" in bad_commit.stderr
    assert git(repo, "rev-list", "--all", "--count").stdout.strip() == "0"

    # Once fixed, the same native pre-commit hook allows the disposable commit.
    source.write_text("def works():\n    return 1\n")
    assert git(repo, "add", "broken.py").returncode == 0
    good_commit = git(
        repo, "-c", "user.name=Fixture Author",
        "-c", "user.email=fixture@example.com",
        "commit", "-m", "test: accept valid Python syntax",
    )
    assert good_commit.returncode == 0, good_commit.stderr
    assert git(repo, "rev-list", "--all", "--count").stdout.strip() == "1"


def test_checked_in_hooks_are_executable_and_have_no_extra_manager():
    assert not (HOOKS / "pre-push").exists(), "Full CI must never run automatically during push"
    for name in ("pre-commit",):
        path = HOOKS / name
        assert path.is_file()
        assert path.stat().st_mode & 0o111
        text = path.read_text()
        assert "scripts/dev/git_validation.py" in text
        assert "--no-verify" not in text
        assert "husky" not in text.lower()
        assert "lefthook" not in text.lower()
