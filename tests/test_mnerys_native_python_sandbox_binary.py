"""The native Python sandbox must launch its already-mounted canonical base.

No host Python distribution or user directory is made visible by these tests.
"""
import os
import sys

import pytest

from src.agent_tools.subprocess_tools import _sandbox_python_base_executable


def test_python_base_resolves_symlinked_interpreter_ancestors(tmp_path, monkeypatch):
    canonical = tmp_path / "runtime"
    (canonical / "bin").mkdir(parents=True)
    binary = canonical / "bin" / "python3.11"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(sys, "base_prefix", str(alias))
    monkeypatch.setattr(sys, "_base_executable", str(alias / "bin" / "python3.11"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "private-venv" / "bin" / "python"))
    assert _sandbox_python_base_executable() == str(binary.resolve())


def test_python_base_refuses_unmounted_private_binary(tmp_path, monkeypatch):
    canonical = tmp_path / "runtime"
    canonical.mkdir()
    foreign = tmp_path / "other-private-directory" / "bin" / "python3.11"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\nexit 0\n")
    foreign.chmod(0o755)
    monkeypatch.setattr(sys, "base_prefix", str(canonical))
    monkeypatch.setattr(sys, "_base_executable", str(foreign))
    with pytest.raises(ValueError, match="trusted sandbox mounts"):
        _sandbox_python_base_executable()


def test_python_base_rejects_missing_binary_in_known_prefix(tmp_path, monkeypatch):
    canonical = tmp_path / "runtime"
    canonical.mkdir()
    monkeypatch.setattr(sys, "base_prefix", str(canonical))
    monkeypatch.setattr(sys, "_base_executable", str(canonical / "bin" / "python3.11"))
    with pytest.raises(ValueError, match="trusted sandbox mounts"):
        _sandbox_python_base_executable()
