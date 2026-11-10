"""Conservative local test selection; no imports of application code.

Static imports cover ordinary dependencies. Literal source references cover
AST/exec/browser fixtures. Explicit contract mappings cover semantic coupling
that cannot be inferred from imports. Unmapped executable changes fail closed.
"""
from __future__ import annotations

import ast
from fnmatch import fnmatchcase
import json
from pathlib import Path, PurePosixPath
import re


class SelectionError(ValueError):
    pass


def module_name(path: str) -> str:
    value = path.removesuffix(".py").replace("/", ".")
    return value.removesuffix(".__init__")


def dependencies(path: str, source: str, modules: dict[str, str], paths: set[str], references: dict[str, set[str]]) -> set[str]:
    result = set()
    if path.endswith(".py"):
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            raise SelectionError(f"Cannot analyze invalid Python: {path}:{exc.lineno}") from exc
        package = module_name(path).split(".")
        if not path.endswith("/__init__.py"):
            package.pop()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = package[:len(package) - node.level + 1] if node.level else []
                base = ".".join(prefix + ([node.module] if node.module else []))
                names = [base] + [f"{base}.{item.name}" for item in node.names]
            for name in names:
                # Include parent package initializers as well as the module.
                parts = name.split(".")
                for end in range(1, len(parts) + 1):
                    target = modules.get(".".join(parts[:end]))
                    if target and target != path:
                        result.add(target)
    # Tests routinely read/compile source instead of importing it. Retain
    # basename matches too, for Path(root) / 'src' / 'module.py' expressions.
    literals = set(re.findall(r"['\"]([^'\"\n]+)['\"]", source))
    for literal in literals:
        result.update(references.get(literal, ()))
    result.discard(path)
    if path.endswith((".js", ".mjs", ".cjs")):
        for literal in literals:
            if literal.startswith("."):
                resolved = (PurePosixPath(path).parent / literal)
                # Normalize '..' without touching the real filesystem.
                stack = []
                for part in resolved.parts:
                    if part == ".." and stack:
                        stack.pop()
                    elif part != ".":
                        stack.append(part)
                target = "/".join(stack)
                if target in paths:
                    result.add(target)
    return result


def select_tests(root: Path, changed: list[str], paths: list[str], contracts: dict) -> dict:
    paths_set = set(paths) | set(changed)
    test_paths = {p for p in paths if p.startswith("tests/") and (
        (p.endswith(".py") and (PurePosixPath(p).name.startswith("test_") or p.endswith("_test.py")))
        or p.endswith(".test.mjs")
    )}
    modules = {module_name(p): p for p in paths_set if p.endswith(".py")}
    references: dict[str, set[str]] = {}
    for path in paths_set:
        for key in (path, module_name(path), PurePosixPath(path).name):
            references.setdefault(key, set()).add(path)
    reverse: dict[str, set[str]] = {}
    for path in sorted(paths_set):
        file = root / path
        if not file.is_file() or not path.endswith((".py", ".js", ".mjs", ".cjs")):
            continue
        if not file.resolve().is_relative_to(root.resolve()):
            raise SelectionError(f"Source reference escapes repository: {path}")
        for dependency in dependencies(path, file.read_text(encoding="utf-8"), modules, paths_set, references):
            reverse.setdefault(dependency, set()).add(path)
    reasons: dict[str, set[str]] = {}
    unmapped, full_required, persistence, ci = [], [], set(), set()
    for change in sorted(set(changed)):
        selected = set()
        matched_contracts = [contract for contract in contracts.get("contracts", [])
            if any(fnmatchcase(change, pattern) for pattern in contract["sources"])]
        if any(fnmatchcase(change, pattern) for pattern in contracts.get("full_required", [])):
            full_required.append(change)
        frontier, visited = [change], set()
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            if current in test_paths:
                selected.add(current)
            for dependent in reverse.get(current, ()):
                # A mapped seam has explicit integration/negative acceptance
                # tests. Still include every direct source fixture and every
                # test importing it, including dependent test fixtures, but
                # do not fan out through the whole application entry point.
                # Unmapped modules retain unrestricted transitive analysis.
                if not matched_contracts or dependent.startswith("tests/"):
                    frontier.append(dependent)
        for contract in matched_contracts:
            for pattern in contract["tests"]:
                matches = {p for p in test_paths if fnmatchcase(p, pattern)}
                if not matches:
                    raise SelectionError(f"Contract {contract['name']} has no tests for {pattern}")
                selected.update(matches)
            persistence.update(contract.get("persistence", []))
            ci.update(contract.get("ci", []))
        if not selected and change not in full_required and not any(
            fnmatchcase(change, pattern) for pattern in contracts.get("static_only", [])
        ):
            unmapped.append(change)
        for test in selected:
            reasons.setdefault(test, set()).add(change)
    return {
        "changed": sorted(set(changed)),
        "python_tests": sorted(p for p in reasons if p.endswith(".py")),
        "node_tests": sorted(p for p in reasons if p.endswith(".mjs")),
        "reasons": {p: sorted(values) for p, values in sorted(reasons.items())},
        "unmapped": unmapped, "full_required": full_required,
        "persistence": sorted(persistence), "ci": sorted(ci),
    }


def load_contracts(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SelectionError("Unsupported affected-test contract map")
    return value
