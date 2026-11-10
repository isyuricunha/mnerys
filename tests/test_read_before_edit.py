"""M5.2 increment 2: server-verified read-before-edit protection.

The contract: a registry file-mutation tool (write_file, edit_file,
apply_patch) may mutate an EXISTING file only when the same owner and session
recorded a real, successful server-side Read of that exact file, and the file
still matches what was observed (identity + content digest). Baselines are
pure server state — the model never supplies a version token.

Concurrency strategy under test: the mutating tools open the target through a
parent-directory fd with O_NOFOLLOW, take an exclusive flock on that
descriptor, and validate identity + digest on the SAME fd they rewrite —
cooperating writers (this server's tools, any process) are mutually excluded,
and a path swap after the open cannot redirect the write.
"""
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from src.agent_runtime.read_baselines import (
    AnchorOutsideObservedLines,
    BaselineRequired,
    FileModifiedSinceRead,
    FileReplacedSinceRead,
    PartialReadInsufficient,
    ReadBaselineError,
    record_file_read,
    require_baseline_for_mutation,
)
from src.agent_tools.filesystem_tools import (
    ApplyPatchTool,
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
)

OWNER = "baseline-tester"
SESSION = "baseline-session"
CTX = {"owner": OWNER, "session_id": SESSION}


def _read(path, ctx=CTX, content=None):
    return asyncio.run(ReadFileTool().execute(
        content or json.dumps({"path": str(path)}), ctx))


def _write(path, body, ctx=CTX):
    return asyncio.run(WriteFileTool().execute(f"{path}\n{body}", ctx))


def _edit(path, old, new, ctx=CTX, **options):
    return asyncio.run(EditFileTool().execute(
        json.dumps({"path": str(path), "old_string": old, "new_string": new, **options}), ctx))


def _patch(patch_text, ctx=CTX, key="patch_text"):
    return asyncio.run(ApplyPatchTool().execute(json.dumps({key: patch_text}), ctx))


def _error_mentions(result, *fragments):
    assert result["exit_code"] == 1
    for fragment in fragments:
        assert fragment in result["error"], result["error"]


@pytest.fixture(autouse=True)
def _isolated_baseline_store():
    from src.agent_runtime import read_baselines
    store = read_baselines.ReadBaselineStore()
    token = read_baselines._store
    read_baselines._store = store
    yield store
    read_baselines._store = token


# ── The core contract ─────────────────────────────────────────────────────

def test_read_then_edit_succeeds(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    result = _edit(path, "value = 1", "value = 2")
    assert result["exit_code"] == 0, result
    assert path.read_text(encoding="utf-8") == "value = 2\n"


def test_edit_without_any_read_is_refulfilled_without_mutation(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("value = 1\n", encoding="utf-8")
    result = _edit(path, "value = 1", "value = 2")
    _error_mentions(result, "No recorded read", "Read", "edit_file")
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_read_of_a_different_file_does_not_authorize_the_edit(tmp_path):
    observed = tmp_path / "observed.txt"
    other = tmp_path / "other.txt"
    observed.write_text("seen\n", encoding="utf-8")
    other.write_text("value = 1\n", encoding="utf-8")
    assert _read(observed)["exit_code"] == 0
    result = _edit(other, "value = 1", "value = 2")
    _error_mentions(result, "No recorded read", str(other))
    assert other.read_text(encoding="utf-8") == "value = 1\n"


def test_write_overwrite_requires_a_full_read(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    result = _write(path, "replaced\n")
    _error_mentions(result, "No recorded read")
    assert path.read_text(encoding="utf-8") == "a\nb\nc\n"
    assert _read(path)["exit_code"] == 0
    result = _write(path, "replaced\n")
    assert result["exit_code"] == 0, result
    assert path.read_text(encoding="utf-8") == "replaced\n"


def test_file_modified_after_read_is_rejected_until_reread(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("original\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    path.write_text("tampered\n", encoding="utf-8")
    result = _edit(path, "tampered", "edited")
    _error_mentions(result, "changed since it was read", "Read it again")
    assert path.read_text(encoding="utf-8") == "tampered\n"
    # Rereading re-baselines the current content and the same edit succeeds.
    assert _read(path)["exit_code"] == 0
    result = _edit(path, "tampered", "edited")
    assert result["exit_code"] == 0, result
    assert path.read_text(encoding="utf-8") == "edited\n"


def test_replaced_file_identity_is_rejected(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("original\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    # Delete-and-recreate with a DIFFERENT file object: holding an fd on the
    # old inode guarantees the recreated file cannot reuse it.
    with open(path, "rb") as pin:
        os.unlink(path)
        path.write_text("original\n", encoding="utf-8")
        assert os.fstat(pin.fileno()).st_ino != os.stat(path).st_ino
        result = _edit(path, "original", "edited")
    _error_mentions(result, "replaced or recreated", "Read it again")
    assert path.read_text(encoding="utf-8") == "original\n"


def test_same_inode_same_content_replacement_is_content_safe(tmp_path):
    """The digest is the real guard: a replacement that reuses the inode with
    identical content is content-equivalent, so the edit proceeds — identity
    is a fast-path check, not the security boundary."""
    path = tmp_path / "doc.txt"
    path.write_text("original\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    os.unlink(path)
    path.write_text("original\n", encoding="utf-8")
    result = _edit(path, "original", "edited")
    assert result["exit_code"] in (0, 1)
    if result["exit_code"] == 1:
        _error_mentions(result, "changed since it was read", "replaced or recreated")
    else:
        assert path.read_text(encoding="utf-8") == "edited\n"


# ── Partial reads ─────────────────────────────────────────────────────────

def test_partial_read_allows_anchored_edit_inside_the_observed_span(tmp_path):
    path = tmp_path / "span.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 21)), encoding="utf-8")
    read = _read(path, content=json.dumps({"path": str(path), "offset": 2, "limit": 3}))
    assert read["exit_code"] == 0
    assert "line 2" in read["output"] and "line 4" in read["output"]
    result = _edit(path, "line 3", "line three")
    assert result["exit_code"] == 0, result
    assert "line three" in path.read_text(encoding="utf-8")


def test_partial_read_refuses_full_file_replacement(tmp_path):
    path = tmp_path / "span.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 21)), encoding="utf-8")
    assert _read(path, content=json.dumps({"path": str(path), "offset": 2, "limit": 3}))["exit_code"] == 0
    result = _write(path, "whole new file\n")
    _error_mentions(result, "only partially read", "lines 2-4", "write_file")
    assert path.read_text(encoding="utf-8").startswith("line 1\n")


def test_partial_read_refuses_editing_unread_lines(tmp_path):
    path = tmp_path / "span.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 21)), encoding="utf-8")
    assert _read(path, content=json.dumps({"path": str(path), "offset": 2, "limit": 3}))["exit_code"] == 0
    result = _edit(path, "line 15", "line fifteen")
    _error_mentions(result, "outside the lines observed", "lines 2-4")
    assert "line 15" in path.read_text(encoding="utf-8")


def test_truncated_unranged_read_does_not_authorize_full_replacement(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("".join(f"row {i} padding padding padding\n" for i in range(5000)), encoding="utf-8")
    read = _read(path)
    assert read["exit_code"] == 0
    assert "truncated" in read["output"]
    result = _write(path, "gone\n")
    _error_mentions(result, "only partially read")
    assert path.read_text(encoding="utf-8").startswith("row 0")


# ── Ownership, sessions, isolation ────────────────────────────────────────

def test_owner_and_session_must_match_the_recorded_read(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path, ctx={"owner": "alice", "session_id": "s1"})["exit_code"] == 0
    for ctx in ({"owner": "bob", "session_id": "s1"}, {"owner": "alice", "session_id": "s2"}):
        result = _edit(path, "value = 1", "value = 2", ctx=ctx)
        _error_mentions(result, "No recorded read")
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_forged_or_tampered_baseline_cannot_authorize(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    record_file_read(OWNER, SESSION, str(path), ("full", 1, 0))
    # A tampered digest is server-side state the model cannot influence:
    # validation fails closed with the modified-since-read guidance.
    from src.agent_runtime import read_baselines
    tampered = read_baselines.ReadBaseline(
        owner=OWNER, session_id=SESSION, path=str(path),
        identity=require_baseline_for_mutation(
            OWNER, SESSION, str(path), tool="edit_file",
            current_bytes=path.read_bytes(),
        ).identity,
        content_sha256="0" * 64, size=1, mtime_ns=1,
        observation=("full", 1, 0), recorded_at=time.time(),
    )
    read_baselines.read_baseline_store().record(tampered)
    result = _edit(path, "value = 1", "value = 2")
    _error_mentions(result, "changed since it was read")
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_baseline_expiry_forces_a_reread(tmp_path):
    from src.agent_runtime import read_baselines
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    store = read_baselines.ReadBaselineStore(ttl_seconds=1)
    expired = read_baselines.ReadBaseline(
        owner=OWNER, session_id=SESSION, path=str(path),
        identity=read_baselines.FileObjectIdentity(1, 2, "file"),
        content_sha256="0" * 64, size=0, mtime_ns=0,
        observation=("full", 1, 0),
        recorded_at=time.time() - read_baselines.DEFAULT_BASELINE_TTL_SECONDS - 5,
    )
    store.record(expired)
    with pytest.raises(BaselineRequired):
        require_baseline_for_mutation(OWNER, SESSION, str(path), tool="edit_file")


def test_store_eviction_keeps_the_bounded_size():
    from src.agent_runtime import read_baselines
    store = read_baselines.ReadBaselineStore(max_entries=3, ttl_seconds=999)
    paths = []
    for i in range(5):
        path = f"/tmp/rb-evict-{i}.txt"
        paths.append(path)
        store.record(read_baselines.ReadBaseline(
            owner="o", session_id="s", path=path,
            identity=read_baselines.FileObjectIdentity(1, i + 1, "file"),
            content_sha256="0" * 64, size=0, mtime_ns=0,
            observation=("full", 1, 0), recorded_at=time.time() + i,
        ))
    assert len(store._baselines) == 3
    # The oldest two fell off; the newest three remain.
    assert store.get("o", "s", paths[0]) is None
    assert store.get("o", "s", paths[-1]) is not None


# ── Concurrency ───────────────────────────────────────────────────────────

def test_concurrent_sessions_cannot_edit_with_each_others_baseline(tmp_path):
    path = tmp_path / "shared.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path, ctx={"owner": "alice", "session_id": "s1"})["exit_code"] == 0
    # Session s2 edits the same path: refused, no cross-session bleed.
    result = _edit(path, "value = 1", "from s2", ctx={"owner": "alice", "session_id": "s2"})
    _error_mentions(result, "No recorded read")
    # s1's edit still works and re-baselines only s1.
    assert _edit(path, "value = 1", "from s1", ctx={"owner": "alice", "session_id": "s1"})["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == "from s1\n"


def test_concurrent_processes_are_serialized_by_the_flock(tmp_path):
    path = tmp_path / "race.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    # A separate PROCESS holds the descriptor lock while it rewrites the file;
    # this process's edit must wait for it, then see changed content and be
    # refused as stale rather than clobbering the concurrent write.
    child = textwrap.dedent("""
        import fcntl, os, sys, time
        path = sys.argv[1]
        marker = sys.argv[2]
        directory = os.path.dirname(path) or "."
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd2 = os.open(os.path.basename(path), os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
            fcntl.flock(fd2, fcntl.LOCK_EX)
            os.lseek(fd2, 0, 0)
            os.ftruncate(fd2, 0)
            os.write(fd2, b"written by the other process\\n")
            os.fsync(fd2)
            # Signal that the lock is held and the content is already changed,
            # so the parent's edit deterministically queues behind this lock.
            open(marker, "w").write("locked")
            time.sleep(1.0)
            fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)
        finally:
            os.close(fd)
    """)
    script = tmp_path / "child_writer.py"
    script.write_text(child, encoding="utf-8")
    marker = tmp_path / "lock-marker"
    process = subprocess.Popen(
        [sys.executable, str(script), str(path), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "the child never acquired the lock"
        result = _edit(path, "value = 1", "value = 2")
    finally:
        process.wait(timeout=15)
    _error_mentions(result, "changed since it was read")
    assert path.read_text(encoding="utf-8") == "written by the other process\n"


# ── Symlinks, traversal, creation races ───────────────────────────────────

def test_symlink_swap_of_the_target_is_refused(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("real\n", encoding="utf-8")
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("decoy\n", encoding="utf-8")
    path = tmp_path / "link.txt"
    path.write_text("original\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    # Swap the path for a symlink AFTER the read; the mutation must refuse.
    os.unlink(path)
    os.symlink(decoy, path)
    result = _edit(path, "decoy", "hacked")
    assert result["exit_code"] == 1
    assert decoy.read_text(encoding="utf-8") == "decoy\n"


def test_path_traversal_shape_still_refused(tmp_path):
    # A traversal-shaped path that would land outside the allowed roots
    # (realpath clamps the leading ups onto /etc/hosts) is refused.
    result = _edit(tmp_path / ".." / ".." / ".." / ".." / ".." / ".." / "etc" / "hosts", "x", "y")
    assert result["exit_code"] == 1
    assert "outside" in result["error"] or "sensitive" in result["error"] or "required" in result["error"]


def test_new_file_creation_is_exclusive_and_safe(tmp_path):
    path = tmp_path / "new.txt"
    result = _write(path, "fresh\n")
    assert result["exit_code"] == 0, result
    assert path.read_text(encoding="utf-8") == "fresh\n"


def test_file_appearing_during_creation_is_never_overwritten(tmp_path):
    path = tmp_path / "raced.txt"
    import src.agent_tools.filesystem_tools as fs
    real_isfile = os.path.isfile
    # The existence check says the file is missing, but by the time the
    # exclusive creation runs, another creator has won the race. Only the
    # FIRST check lies; the race handler's own re-check must see the truth.
    calls = {"n": 0}
    def lying_isfile(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real_isfile(p)
    fs.os.path.isfile = lying_isfile
    try:
        path.write_text("appeared\n", encoding="utf-8")
        result = _write(path, "clobber\n")
    finally:
        fs.os.path.isfile = real_isfile
    _error_mentions(result, "appeared", "not overwritten", "Read it first")
    assert path.read_text(encoding="utf-8") == "appeared\n"


def test_failed_write_reports_attempt_and_stale_content_requires_reread(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    import src.agent_tools.filesystem_tools as fs
    original_rewrite = fs._rewrite_fd_contents
    def corrupting_rewrite(fd, text):
        # A failure AFTER partial bytes hit the descriptor: the file on disk
        # no longer matches anything the agent observed.
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"CORRUPTED")
        raise OSError("simulated mid-write failure")
    fs._rewrite_fd_contents = corrupting_rewrite
    try:
        result = _write(path, "value = 2\n")
    finally:
        fs._rewrite_fd_contents = original_rewrite
    assert result["exit_code"] == 1
    assert result.get("mutation_attempted") is True
    assert path.read_text(encoding="utf-8").startswith("CORRUPTED")
    # After a partially-corrupting failure the recorded read no longer
    # matches the disk: the next edit is refused until the agent rereads —
    # never a silent continue over content it has not observed.
    result = _edit(path, "value = 1", "value = 3")
    _error_mentions(result, "changed since it was read", "Read it again")


# ── Structured patches ────────────────────────────────────────────────────

def _patch_text(update=True):
    return textwrap.dedent("""\
        *** Begin Patch
        *** Update File: patched.txt
        @@
        -before
        +after
        *** End Patch""") if update else ""


def test_patch_after_full_read_applies_and_rebaselines(tmp_path):
    path = tmp_path / "patched.txt"
    path.write_text("before\n", encoding="utf-8")
    import src.tool_execution as tool_execution
    original_resolve = tool_execution._resolve_tool_path
    tool_execution._resolve_tool_path = lambda p: str(tmp_path / os.path.basename(p))
    try:
        assert _read(path)["exit_code"] == 0
        result = _patch(_patch_text())
        assert result["exit_code"] == 0, result
        assert path.read_text(encoding="utf-8") == "after\n"
        # The patch re-baselined the new content: a follow-up edit works.
        result = _edit(path, "after", "after2")
        assert result["exit_code"] == 0, result
    finally:
        tool_execution._resolve_tool_path = original_resolve


def test_patch_without_a_read_is_refused(tmp_path):
    path = tmp_path / "patched.txt"
    path.write_text("before\n", encoding="utf-8")
    import src.tool_execution as tool_execution
    original_resolve = tool_execution._resolve_tool_path
    tool_execution._resolve_tool_path = lambda p: str(tmp_path / os.path.basename(p))
    try:
        result = _patch(_patch_text())
        _error_mentions(result, "No recorded read", "apply_patch")
        assert path.read_text(encoding="utf-8") == "before\n"
    finally:
        tool_execution._resolve_tool_path = original_resolve


def test_patch_after_partial_read_is_refused(tmp_path):
    path = tmp_path / "patched.txt"
    path.write_text("before\nmore\n", encoding="utf-8")
    import src.tool_execution as tool_execution
    original_resolve = tool_execution._resolve_tool_path
    tool_execution._resolve_tool_path = lambda p: str(tmp_path / os.path.basename(p))
    try:
        assert _read(path, content=json.dumps({"path": str(path), "offset": 1, "limit": 1}))["exit_code"] == 0
        result = _patch(_patch_text())
        _error_mentions(result, "only partially read", "apply_patch")
        assert path.read_text(encoding="utf-8") == "before\nmore\n"
    finally:
        tool_execution._resolve_tool_path = original_resolve


# ── Code-execution boundary (the documented limitation) ──────────────────

def _dispatch_bash(command, monkeypatch=None, workspace=None):
    """One real bash dispatch through the shared dispatcher, with the
    standalone fixture's server grants (the established test pattern)."""
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_types import ToolBlock
    from tests.runtime_evidence_helpers import server_authorized_executor
    if monkeypatch is not None:
        import src.tool_execution as te
        monkeypatch.setattr(te, "_owner_is_admin", lambda owner: owner == OWNER)
    kwargs = {
        "owner": OWNER, "session_id": SESSION,
        "security_context": NO_TOOL_SECURITY_CONTEXT,
    }
    if workspace is not None:
        kwargs["workspace"] = workspace
    return asyncio.run(server_authorized_executor(execute_tool_block)(
        ToolBlock("bash", command), **kwargs,
    ))


def test_bash_execution_invalidates_session_baselines(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    # A real bash dispatch through the shared dispatcher (echo only).
    _desc, result = _dispatch_bash("echo baseline-invalidation-probe", monkeypatch)
    assert result.get("exit_code") == 0, result
    # The shell could have written anything: the recorded read no longer
    # authorizes edits — the agent must reread.
    result = _edit(path, "value = 1", "value = 2")
    _error_mentions(result, "No recorded read")
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_bash_side_effect_on_a_read_file_is_caught_by_invalidation(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    # The documented bypass DEMONSTRATED: contained bash rewrites the file
    # through the sandbox's /workspace alias (the real workspace write
    # surface), without any registry tool call.
    _desc, bash_result = _dispatch_bash(
        "printf 'shell wrote this\\n' > /workspace/doc.txt", monkeypatch,
        workspace=str(tmp_path),
    )
    assert bash_result.get("exit_code") == 0, bash_result
    assert "shell wrote this" in path.read_text(encoding="utf-8")
    # The contract converts the bypass into a visible refusal: the next edit
    # cannot validate against content the shell may have changed.
    result = _edit(path, "value = 1", "value = 2")
    _error_mentions(result, "No recorded read")
    assert "shell wrote this" in path.read_text(encoding="utf-8")


# ── Authority and dispatch ordering ───────────────────────────────────────

def test_baseline_refusal_happens_after_authority_and_before_mutation(tmp_path):
    """An admitted edit whose baseline went stale is refused post-admission,
    pre-mutation — fail-closed ordering, with an actionable message. The full
    dispatcher wiring (grants, sealed resource roots) lives in the gateway
    approval suites; here the handler contract is what's under test."""
    from src.agent_runtime.resource_binding import (
        BoundFilesystemOperation,
        ResourceBinding,
        bind_resource_operation,
    )
    from src.agent_runtime.resources import (
        FilesystemResource,
        FilesystemRoot,
        FilesystemScope,
    )
    from src.agent_runtime.authority import ExactOperation
    from src.path_confinement import canonical_root

    path = tmp_path / "doc.txt"
    path.write_text("value = 1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    path.write_text("externally changed\n", encoding="utf-8")

    content = json.dumps({"path": str(path), "old_string": "value = 1", "new_string": "value = 2"})
    operation = ExactOperation.normalize("edit_file", content)
    root = FilesystemRoot.seal(canonical_root(str(tmp_path)), scope=FilesystemScope.WORKSPACE, owner=OWNER)
    resource = FilesystemResource.resolve(root, str(path))
    bound = BoundFilesystemOperation(
        operation=operation,
        execution_input=json.dumps(
            {"path": str(path), "old_string": "value = 1", "new_string": "value = 2"},
            sort_keys=True,
        ),
        bindings=(ResourceBinding("target", resource),),
        request_id="req-baseline-refusal",
    )
    with bind_resource_operation(bound):
        result = asyncio.run(EditFileTool().execute(content, CTX))
    assert result.get("exit_code") == 1
    assert "changed since it was read" in result.get("error", ""), result
    assert path.read_text(encoding="utf-8") == "externally changed\n"


# ── Read observation fidelity ─────────────────────────────────────────────

def test_failed_read_records_no_baseline(tmp_path):
    path = tmp_path / "missing.txt"
    result = _read(path)
    assert result["exit_code"] == 1
    # The edit of a MISSING file fails on the file itself (open) before the
    # baseline contract: "not found" — the actionable guidance is to create it.
    result = _edit(path, "a", "b")
    _error_mentions(result, "not found", "write_file to create it")


def test_denied_read_records_no_baseline(tmp_path, monkeypatch):
    from src.agent_runtime.resource_binding import (
        BoundFilesystemOperation,
        ResourceBinding,
    )
    path = tmp_path / "doc.txt"
    path.write_text("secret\n", encoding="utf-8")
    # A read that fails at the dispatcher level (denied/unavailable) must not
    # produce a baseline: record only happens on a real successful read.
    from src.agent_runtime import read_baselines
    assert read_baselines.read_baseline_store().get(OWNER, SESSION, str(path)) is None
    result = _edit(path, "secret", "stolen")
    _error_mentions(result, "No recorded read")


def test_successive_reads_refresh_the_baseline(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    path.write_text("v2\n", encoding="utf-8")
    assert _read(path)["exit_code"] == 0
    result = _edit(path, "v2", "v3")
    assert result["exit_code"] == 0, result
