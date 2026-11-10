"""M5.2 increment 4: derived-artifact writer publication safety.

The contract: derived-artifact writers (inspect_media still/clip/SVG exports,
transcribe_media transcripts) publish generated outputs through the shared
publication boundary instead of writing destination paths directly.

* Creating a NEW artifact needs no prior read and is exclusive — a file that
  appears at the destination is never overwritten.
* Replacing an EXISTING user file requires a real, server-recorded read
  baseline (same owner and session) with unchanged identity and content —
  enforced through the same read_baselines store the registry text tools use.
* Internal disposable caches publish without a baseline but never through a
  symlink.
* Publication is staged beside the destination and atomic: a symlink at the
  destination is refused, never followed or written through.
"""
import asyncio
import fcntl
import json
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from src.agent_tools.artifact_publication import (
    ArtifactPublicationError,
    DestinationAppeared,
    DestinationReplacedSinceValidation,
    INTERNAL_CACHE,
    UnsafeDestination,
    USER_FILE,
    create_exclusive_file,
    ensure_destination_publishable,
    publish_staged_artifact,
    staged_artifact_path,
)
from src.agent_tools.filesystem_tools import ReadFileTool
from src.agent_tools.media_tools import InspectMediaTool, TranscribeMediaTool
from src.agent_runtime import read_baselines
from src.agent_runtime.read_baselines import (
    BaselineRequired,
    FileModifiedSinceRead,
    FileReplacedSinceRead,
    PartialReadInsufficient,
    ReadBaselineError,
    record_file_read,
)
from src.tool_execution import _active_workspace

OWNER = "artifact-tester"
SESSION = "artifact-session"
CTX = {"owner": OWNER, "session_id": SESSION}


@pytest.fixture(autouse=True)
def _isolated_baseline_store():
    store = read_baselines.ReadBaselineStore()
    token = read_baselines._store
    read_baselines._store = store
    yield store
    read_baselines._store = token


@pytest.fixture(autouse=True)
def _workspace(request):
    tmp = request.getfixturevalue("tmp_path")
    token = _active_workspace.set(str(tmp))
    yield tmp
    _active_workspace.reset(token)


def _read(path, ctx=CTX):
    return asyncio.run(ReadFileTool().execute(
        json.dumps({"path": str(path)}), ctx))


def _inspect(args, ctx=CTX):
    return asyncio.run(InspectMediaTool().execute(json.dumps(args), ctx))


def _publish(owner, session, destination, staged, **kwargs):
    return publish_staged_artifact(
        owner, session, destination, staged,
        tool="test_writer", **kwargs
    )


# ── Publication boundary: new artifacts ───────────────────────────────────

def test_new_artifact_publishes_exclusively_without_a_read(tmp_path):
    destination = tmp_path / "out.png"
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"generated-bytes")
        assert _publish(OWNER, SESSION, destination, staged) == "created"
    assert destination.read_bytes() == b"generated-bytes"
    assert not list(tmp_path.glob(".odysseus-publish-*"))


def test_destination_appearing_during_generation_is_never_overwritten(tmp_path):
    destination = tmp_path / "out.png"
    ensure_destination_publishable(OWNER, SESSION, destination, tool="test_writer")
    # Another creator wins the race while the artifact is being generated.
    destination.write_bytes(b"someone-else")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"generated-bytes")
        # The appeared file has no recorded read, so publication is refused
        # and the file is left exactly as the other creator wrote it.
        with pytest.raises(ReadBaselineError) as excinfo:
            _publish(OWNER, SESSION, destination, staged)
    assert "No recorded read" in str(excinfo.value)
    assert destination.read_bytes() == b"someone-else"


def test_link_race_is_detected_as_appeared(tmp_path, monkeypatch):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"someone-else")
    # Force the narrow lstat→link race deterministically: publication's
    # existence probe misses once, the exclusive link hits EEXIST, and the
    # appeared destination is refused instead of overwritten.
    from src.agent_tools import artifact_publication

    real_lstat = artifact_publication._lstat_destination
    skip = {"probe": 1}

    def absent_once(dir_fd, name):
        result = real_lstat(dir_fd, name)
        if result is not None and skip["probe"]:
            skip["probe"] = 0
            return None
        return result

    monkeypatch.setattr(artifact_publication, "_lstat_destination", absent_once)
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"generated-bytes")
        with pytest.raises(DestinationAppeared):
            _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"someone-else"


def test_appeared_symlink_at_destination_is_refused_not_followed(tmp_path):
    destination = tmp_path / "out.png"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    ensure_destination_publishable(OWNER, SESSION, destination, tool="test_writer")
    os.symlink(victim, destination)
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"generated-bytes")
        with pytest.raises(UnsafeDestination):
            _publish(OWNER, SESSION, destination, staged)
    assert victim.read_text(encoding="utf-8") == "keep"


def test_symlink_destination_is_refused_and_target_untouched(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    destination = tmp_path / "link.png"
    os.symlink(target, destination)
    with pytest.raises(UnsafeDestination):
        ensure_destination_publishable(OWNER, SESSION, destination, tool="test_writer")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"generated-bytes")
        with pytest.raises(UnsafeDestination):
            _publish(OWNER, SESSION, destination, staged)
    assert target.read_text(encoding="utf-8") == "original"


def test_directory_destination_is_refused(tmp_path):
    destination = tmp_path / "adir"
    destination.mkdir()
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"x")
        with pytest.raises(UnsafeDestination):
            _publish(OWNER, SESSION, destination, staged)


# ── Publication boundary: replacing existing files ────────────────────────

def test_replace_after_recorded_read_publishes_and_rebaselines(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        assert _publish(OWNER, SESSION, destination, staged) == "replaced"
    assert destination.read_bytes() == b"new-bytes"
    # The publication re-baselined the written content: a follow-up replace
    # validates against it without forcing a fresh read.
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"third")
        assert _publish(OWNER, SESSION, destination, staged) == "replaced"
    assert destination.read_bytes() == b"third"


def test_replace_without_a_recorded_read_is_refused(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        with pytest.raises(BaselineRequired) as excinfo:
            _publish(OWNER, SESSION, destination, staged)
    assert "No recorded read" in str(excinfo.value)
    assert "Read" in str(excinfo.value)
    assert destination.read_bytes() == b"old-bytes"


def test_stale_content_is_refused_until_reread(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"read-state")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    destination.write_bytes(b"mutated-behind-the-read")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        with pytest.raises(FileModifiedSinceRead):
            _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"mutated-behind-the-read"


def test_replaced_file_is_refused(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"read-state")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    destination.unlink()
    destination.write_bytes(b"different-bytes")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        # Identity or content necessarily differs after a recreate with new
        # bytes; either refusal class is the contract (the exact class
        # depends on whether the filesystem reused the inode).
        with pytest.raises(ReadBaselineError):
            _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"different-bytes"


def test_byte_identical_recreate_is_content_safe(tmp_path):
    # Documented increment-2 semantics shared by the derived boundary: when
    # the path again holds the exact bytes that were read (same identity and
    # digest — the filesystem may reuse the inode), the replace is safe and
    # proceeds; nothing unread can be lost.
    destination = tmp_path / "out.png"
    destination.write_bytes(b"read-state")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    destination.unlink()
    destination.write_bytes(b"read-state")
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        kind = _publish(OWNER, SESSION, destination, staged)
    assert kind == "replaced"
    assert destination.read_bytes() == b"new-bytes"


def test_wrong_owner_and_session_cannot_replace(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        with pytest.raises(BaselineRequired):
            _publish(OWNER, "another-session", destination, staged)
        with pytest.raises(BaselineRequired):
            _publish("another-owner", SESSION, destination, staged)
    assert destination.read_bytes() == b"old-bytes"


def test_forged_baseline_digest_cannot_authorize(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"real-bytes")
    baseline = record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    assert baseline
    # Tamper with the recorded digest server-side: the check must fail on the
    # real content, not trust the store entry's claim.
    tampered = read_baselines.ReadBaseline(
        owner=baseline.owner,
        session_id=baseline.session_id,
        path=baseline.path,
        identity=baseline.identity,
        content_sha256="0" * 64,
        size=baseline.size,
        mtime_ns=baseline.mtime_ns,
        observation=baseline.observation,
        recorded_at=baseline.recorded_at,
    )
    read_baselines.read_baseline_store().record(tampered)
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        with pytest.raises(FileModifiedSinceRead):
            _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"real-bytes"


def test_opaque_observation_does_not_authorize_a_derived_replace(tmp_path):
    destination = tmp_path / "report.pdf"
    destination.write_bytes(b"%PDF-old")
    assert record_file_read(OWNER, SESSION, str(destination), ("opaque", 0, 0))
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"%PDF-new")
        with pytest.raises(PartialReadInsufficient) as excinfo:
            _publish(OWNER, SESSION, destination, staged)
    assert "derived document text" in str(excinfo.value)
    assert destination.read_bytes() == b"%PDF-old"


def test_destination_above_the_baseline_size_bound_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(read_baselines, "DEFAULT_MAX_BASELINE_FILE_BYTES", 8)
    destination = tmp_path / "big.png"
    destination.write_bytes(b"b" * 64)
    # The read path cannot record a baseline for an oversized file.
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0)) is None
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"c" * 8)
        with pytest.raises(BaselineRequired):
            _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"b" * 64


def test_replace_preserves_existing_file_permissions(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    os.chmod(destination, 0o640)
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    with staged_artifact_path(destination) as staged:
        staged.write_bytes(b"new-bytes")
        _publish(OWNER, SESSION, destination, staged)
    assert destination.read_bytes() == b"new-bytes"
    assert stat_mode(destination) == 0o640


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_refused_publication_leaves_no_staged_files_behind(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    with pytest.raises(BaselineRequired):
        with staged_artifact_path(destination) as staged:
            staged.write_bytes(b"new-bytes")
            _publish(OWNER, SESSION, destination, staged)
    assert list(tmp_path.glob(".odysseus-publish-*")) == []


def test_staged_files_keep_the_destination_suffix(tmp_path):
    destination = tmp_path / "clip.mp4"
    with staged_artifact_path(destination) as staged:
        assert staged.suffix == ".mp4"
        assert staged.parent == destination.parent


def test_held_flock_serializes_cooperating_publication(tmp_path):
    destination = tmp_path / "out.png"
    destination.write_bytes(b"old-bytes")
    assert record_file_read(OWNER, SESSION, str(destination), ("full", 1, 0))
    blocker = os.open(destination, os.O_RDONLY)
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def _publish_in_thread():
        started.set()
        try:
            with staged_artifact_path(destination) as staged:
                staged.write_bytes(b"contended")
                outcome["kind"] = _publish(OWNER, SESSION, destination, staged)
            outcome["error"] = None
        except Exception as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    fcntl.flock(blocker, fcntl.LOCK_EX)
    worker = threading.Thread(target=_publish_in_thread)
    worker.start()
    assert started.wait(5)
    worker.join(timeout=1.0)
    # While a cooperating writer holds the destination lock, publication has
    # not completed and nothing was replaced.
    assert not finished.is_set()
    assert destination.read_bytes() == b"old-bytes"
    fcntl.flock(blocker, fcntl.LOCK_UN)
    os.close(blocker)
    assert finished.wait(5)
    worker.join(timeout=5)
    assert outcome["error"] is None, outcome["error"]
    assert outcome["kind"] == "replaced"
    assert destination.read_bytes() == b"contended"


# ── Internal disposable caches ────────────────────────────────────────────

def test_internal_cache_publishes_without_a_baseline(tmp_path):
    destination = tmp_path / ".odysseus" / "transcripts" / "media-abc123.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("stale cache", encoding="utf-8")
    with staged_artifact_path(destination) as staged:
        staged.write_text("fresh cache", encoding="utf-8")
        assert _publish(
            OWNER, SESSION, destination, staged, classification=INTERNAL_CACHE
        ) == "replaced"
    assert destination.read_text(encoding="utf-8") == "fresh cache"


def test_internal_cache_refuses_a_symlink_destination(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    destination = tmp_path / "media-abc123.txt"
    os.symlink(target, destination)
    with staged_artifact_path(destination) as staged:
        staged.write_text("cache", encoding="utf-8")
        with pytest.raises(UnsafeDestination):
            _publish(OWNER, SESSION, destination, staged, classification=INTERNAL_CACHE)
    assert target.read_text(encoding="utf-8") == "keep"


def test_create_exclusive_file_never_overwrites(tmp_path):
    path = tmp_path / "gallery.png"
    create_exclusive_file(path, b"first")
    with pytest.raises(FileExistsError):
        create_exclusive_file(path, b"second")
    assert path.read_bytes() == b"first"


# ── inspect_media end-to-end (real local ffmpeg path) ─────────────────────

def _video(tmp_path: Path, name: str = "source.mp4") -> Path:
    import subprocess
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_inspect_media_creates_a_new_still_artifact_end_to_end(tmp_path):
    source = _video(tmp_path)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 0, result
    assert result["output_path"] == "/workspace/still.png"
    artifact = tmp_path / "still.png"
    assert artifact.is_file()
    with Image.open(artifact) as saved:
        assert saved.size
    assert not list(tmp_path.glob(".odysseus-publish-*"))


def test_inspect_media_refuses_overwrite_without_a_read(tmp_path):
    source = _video(tmp_path)
    destination = tmp_path / "still.png"
    destination.write_bytes(b"precious user file")
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 1
    assert result.get("read_before_edit") is True
    assert "No recorded read" in result["error"]
    assert destination.read_bytes() == b"precious user file"
    assert not list(tmp_path.glob(".odysseus-publish-*"))


def test_inspect_media_replaces_an_existing_file_after_an_authorized_read(tmp_path):
    source = _video(tmp_path)
    destination = tmp_path / "still.png"
    destination.write_bytes(b"old content")
    assert _read(destination)["exit_code"] == 0
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 0, result
    assert destination.read_bytes() != b"old content"
    with Image.open(destination) as saved:
        assert saved.format == "PNG"


def test_inspect_media_overwrite_after_bash_is_refused_until_reread(tmp_path, monkeypatch):
    source = _video(tmp_path)
    destination = tmp_path / "still.png"
    destination.write_bytes(b"old content")
    assert _read(destination)["exit_code"] == 0
    # Code execution invalidates every baseline of the owner/session — the
    # derived writers validate against the same store, so the overwrite is
    # refused until the agent rereads.
    from src.agent_runtime.read_baselines import invalidate_for_code_execution
    invalidate_for_code_execution(OWNER, SESSION)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 1
    assert result.get("read_before_edit") is True
    assert destination.read_bytes() == b"old content"
    assert _read(destination)["exit_code"] == 0
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 0, result


def test_inspect_media_batch_exports_to_distinct_new_destinations(tmp_path):
    source = _video(tmp_path)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "exports": [
            {"timestamp": "0.5", "output_path": "/workspace/f1.png"},
            {"timestamp": "1.5", "output_path": "/workspace/f2.png"},
        ],
    })
    assert result["exit_code"] == 0, result
    assert (tmp_path / "f1.png").is_file()
    assert (tmp_path / "f2.png").is_file()
    assert result["output_paths"] == ["/workspace/f1.png", "/workspace/f2.png"]


def test_inspect_media_batch_refuses_an_existing_unread_destination_before_creating_anything(tmp_path):
    source = _video(tmp_path)
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"keep me")
    result = _inspect({
        "path": "/workspace/source.mp4",
        "exports": [
            {"timestamp": "0.5", "output_path": "/workspace/new1.png"},
            {"timestamp": "1.5", "output_path": "/workspace/existing.png"},
        ],
    })
    assert result["exit_code"] == 1
    assert result.get("read_before_edit") is True
    assert existing.read_bytes() == b"keep me"
    assert not (tmp_path / "new1.png").exists()
    assert not list(tmp_path.glob(".odysseus-publish-*"))


def test_inspect_media_batch_duplicate_destinations_are_refused_upfront(tmp_path):
    source = _video(tmp_path)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "exports": [
            {"timestamp": "0.5", "output_path": "/workspace/same.png"},
            {"timestamp": "1.5", "output_path": "/workspace/same.png"},
        ],
    })
    assert result["exit_code"] == 1
    assert "unique output_path" in result["error"]
    assert not (tmp_path / "same.png").exists()


def test_inspect_media_timestamp_sidecar_overwrite_requires_a_read(tmp_path):
    source = _video(tmp_path)
    destination = tmp_path / "still.png"
    sidecar = tmp_path / "times.txt"
    sidecar.write_text("existing sidecar\n", encoding="utf-8")
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
        "timestamp_path": "/workspace/times.txt",
    })
    assert result["exit_code"] == 1
    assert result.get("read_before_edit") is True
    assert sidecar.read_text(encoding="utf-8") == "existing sidecar\n"
    assert not destination.exists()
    # After reading the sidecar the same call succeeds and preserves the
    # still/clip publication semantics.
    assert _read(sidecar)["exit_code"] == 0
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
        "timestamp_path": "/workspace/times.txt",
    })
    assert result["exit_code"] == 0, result
    assert "00:00:01" in sidecar.read_text(encoding="utf-8")
    assert destination.is_file()


def test_inspect_media_output_path_traversal_is_still_refused(tmp_path):
    source = _video(tmp_path)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/../escaped.png",
    })
    assert result["exit_code"] == 1
    assert "inside the active workspace" in result["error"]
    assert not (tmp_path.parent / "escaped.png").exists()


def test_inspect_media_generator_failure_writes_nothing_and_reports_honestly(tmp_path, monkeypatch):
    from src.agent_tools import media_tools

    source = _video(tmp_path)
    destination = tmp_path / "still.png"
    real_run = media_tools._run

    def failing_still_run(command, timeout=60):
        if "-frames:v" in command:
            return __import__("subprocess").CompletedProcess(command, 1, "", "boom")
        return real_run(command, timeout)

    monkeypatch.setattr(media_tools, "_run", failing_still_run)
    result = _inspect({
        "path": "/workspace/source.mp4",
        "timestamp": "1",
        "output_path": "/workspace/still.png",
    })
    assert result["exit_code"] == 1
    assert "ffmpeg still extraction failed" in result["error"]
    assert "output_path" not in result
    assert not destination.exists()
    assert not list(tmp_path.glob(".odysseus-publish-*"))


def test_inspect_media_svg_render_publishes_to_a_new_destination(tmp_path):
    import shutil

    if not shutil.which("convert"):
        pytest.skip("ImageMagick convert required")
    (tmp_path / "source.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
        encoding="utf-8",
    )
    result = _inspect({
        "path": "/workspace/source.svg",
        "output_path": "/workspace/rendered.png",
    })
    assert result["exit_code"] == 0, result
    artifact = tmp_path / "rendered.png"
    assert artifact.is_file()
    with Image.open(artifact) as saved:
        assert saved.format == "PNG"


# ── transcribe_media destinations ─────────────────────────────────────────

class _FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeWhisperModel:
    def transcribe(self, path, **kwargs):
        segments = [_FakeSegment(0.0, 1.0, "hello"), _FakeSegment(1.0, 2.0, "world")]
        info = type("Info", (), {"language": "en", "language_probability": 0.9})()
        return iter(segments), info


@pytest.fixture
def fake_whisper(monkeypatch):
    import src.agent_tools.media_tools as media_tools
    monkeypatch.setattr(media_tools, "_WHISPER_MODELS", {"base": _FakeWhisperModel()})


def _transcribe(args, ctx=CTX):
    return asyncio.run(TranscribeMediaTool().execute(json.dumps(args), ctx))


def test_transcribe_media_explicit_destination_overwrite_requires_a_read(tmp_path, fake_whisper):
    source = _video(tmp_path)
    destination = tmp_path / "transcript.txt"
    destination.write_text("keep\n", encoding="utf-8")
    result = _transcribe({
        "path": "/workspace/source.mp4",
        "model": "base",
        "output_path": "/workspace/transcript.txt",
    })
    assert result["exit_code"] == 1
    assert result.get("read_before_edit") is True
    assert destination.read_text(encoding="utf-8") == "keep\n"
    assert _read(destination)["exit_code"] == 0
    result = _transcribe({
        "path": "/workspace/source.mp4",
        "model": "base",
        "output_path": "/workspace/transcript.txt",
    })
    assert result["exit_code"] == 0, result
    assert "hello" in destination.read_text(encoding="utf-8")


def test_transcribe_media_internal_cache_publishes_without_any_read(tmp_path, fake_whisper):
    source = _video(tmp_path)
    result = _transcribe({"path": "/workspace/source.mp4", "model": "base"})
    assert result["exit_code"] == 0, result
    cache = tmp_path / ".odysseus" / "transcripts"
    saved = list(cache.glob("*.txt"))
    assert len(saved) == 1
    assert "hello" in saved[0].read_text(encoding="utf-8")
