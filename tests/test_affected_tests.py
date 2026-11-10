"""Behavioral selection tests; no app imports, pytest collection or network."""
from pathlib import Path

import pytest

from scripts.dev.affected_tests import SelectionError, load_contracts, select_tests
from scripts.dev import git_validation as validation


def project(tmp_path, files):
    for path, source in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return list(files)


def test_transitive_imports_relative_imports_and_ast_read_fixtures(tmp_path):
    paths = project(tmp_path, {
        "src/guard.py": "PERMIT = False\n",
        "src/dispatch.py": "from .guard import PERMIT\n",
        "tests/test_execute.py": "from src import dispatch\n",
        "tests/test_ast.py": "from pathlib import Path\nsource = Path('src') / 'guard.py'\n",
        "tests/test_other.py": "def test_other(): pass\n",
    })
    plan = select_tests(tmp_path, ["src/guard.py"], paths, {})
    assert plan["python_tests"] == ["tests/test_ast.py", "tests/test_execute.py"]
    assert plan["reasons"]["tests/test_execute.py"] == ["src/guard.py"]
    assert not plan["unmapped"]


def test_deleted_module_keeps_import_dependents(tmp_path):
    paths = project(tmp_path, {"tests/test_removed.py": "from src.removed import operation\n"})
    plan = select_tests(tmp_path, ["src/removed.py"], paths, {})
    assert plan["python_tests"] == paths


def test_browser_imports_and_source_fixtures(tmp_path):
    paths = project(tmp_path, {
        "static/js/status.js": "export const done = false;",
        "static/js/view.js": "import {done} from './status.js';",
        "tests/view.test.mjs": "const source = new URL('../static/js/view.js', import.meta.url);",
    })
    plan = select_tests(tmp_path, ["static/js/status.js"], paths, {})
    assert plan["node_tests"] == ["tests/view.test.mjs"]


def test_contracts_add_negative_tests_and_persistence_requirements(tmp_path):
    paths = project(tmp_path, {
        "src/store.py": "pass\n", "tests/test_negative.py": "pass\n",
    })
    contracts = {"contracts": [{"name": "audit", "sources": ["src/store.py"],
        "tests": ["tests/test_negative.py"], "persistence": ["sqlite", "postgresql17"],
        "ci": [".github/workflows/postgresql17.yml"]}]}
    plan = select_tests(tmp_path, ["src/store.py"], paths, contracts)
    assert plan["python_tests"] == ["tests/test_negative.py"]
    assert plan["persistence"] == ["postgresql17", "sqlite"]
    assert plan["ci"] == [".github/workflows/postgresql17.yml"]
    contracts["contracts"][0]["tests"] = ["tests/missing.py"]
    with pytest.raises(SelectionError, match="has no tests"):
        select_tests(tmp_path, ["src/store.py"], paths, contracts)


def test_unmapped_and_shared_fixture_changes_never_run_silently(tmp_path):
    paths = project(tmp_path, {"src/new.py": "pass\n", "tests/conftest.py": "pass\n"})
    plan = select_tests(tmp_path, paths, paths, {"full_required": ["tests/conftest.py"]})
    assert plan["unmapped"] == ["src/new.py"]
    assert plan["full_required"] == ["tests/conftest.py"]


def test_invalid_python_blocks_dependency_analysis(tmp_path):
    paths = project(tmp_path, {"src/invalid.py": "def broken("})
    with pytest.raises(SelectionError, match="invalid Python"):
        select_tests(tmp_path, paths, paths, {})


def test_external_source_symlink_is_not_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "private.py"
    outside.write_text("secret = 1")
    (root / "linked.py").symlink_to(outside)
    with pytest.raises(SelectionError, match="escapes repository"):
        select_tests(root, ["linked.py"], ["linked.py"], {})


def test_mapped_seam_keeps_direct_and_fixture_tests_without_app_wide_fanout(tmp_path):
    paths = project(tmp_path, {
        "src/store.py": "pass\n", "app.py": "from src import store\n",
        "tests/test_direct.py": "from src import store\n",
        "tests/test_fixture.py": "from tests import test_direct\n",
        "tests/test_app_unrelated.py": "import app\n",
        "tests/test_integration.py": "pass\n",
    })
    contracts = {"contracts": [{"name": "store", "sources": ["src/store.py"],
        "tests": ["tests/test_integration.py"]}]}
    plan = select_tests(tmp_path, ["src/store.py"], paths, contracts)
    assert plan["python_tests"] == ["tests/test_direct.py", "tests/test_fixture.py", "tests/test_integration.py"]


def test_default_driver_runs_focused_tests_not_shards(monkeypatch):
    plan = {"changed": [], "python_tests": ["tests/test_exact.py"], "node_tests": [],
        "unmapped": [], "full_required": [], "persistence": []}
    calls = []
    monkeypatch.setattr(validation, "affected_plan", lambda **kw: plan)
    monkeypatch.setattr(validation, "python_for_tests", lambda: "test-python")
    monkeypatch.setattr(validation, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(validation.sys, "argv", ["git_validation.py"])
    assert validation.main() == 0
    assert ["test-python", "-m", "pytest", "-q", "tests/test_exact.py"] in calls
    assert not any("--shard" in cmd for cmd in calls)


def test_unmapped_execution_fails_before_test_process(monkeypatch):
    monkeypatch.setattr(validation, "affected_plan", lambda **kw: {
        "python_tests": [], "node_tests": [], "unmapped": ["src/new.py"],
        "full_required": [], "persistence": [],
    })
    monkeypatch.setattr(validation, "run", lambda *a, **kw: pytest.fail("unexpected test process"))
    with pytest.raises(validation.ValidationError, match="requires review"):
        validation.validate_affected()


def test_contract_map_points_at_existing_ci_and_test_files():
    from fnmatch import fnmatchcase
    root = Path(__file__).resolve().parents[1]
    contracts = load_contracts(root / "scripts/dev/test_contracts.json")
    tests = [p.relative_to(root).as_posix() for p in (root / "tests").rglob("*") if p.is_file()]
    for contract in contracts["contracts"]:
        for workflow in contract.get("ci", []):
            assert (root / workflow).is_file(), workflow
        for pattern in contract["tests"]:
            assert any(fnmatchcase(p, pattern) for p in tests), pattern
