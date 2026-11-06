"""Owner-checked image uploads in non-persisting composed turns.

Fixtures simulate the inherited ChatHandler multimodal preprocessing. No
vision model, upload index, database or HTTP app is used.
"""

import hashlib
import os
from types import SimpleNamespace

import pytest

from src import turn_service as turn_module
from src.gateway_admission import AdmissionDenied
from src.gateway_chat_context_types import PreprocessedMessage
from src.gateway_turn_composer import TextTurnRequest, TrustedTextTurnSettings
from src.gateway_turns import GatewayTurns
from src.turn_service import TurnService
from tests.test_mnerys_gateway_turn_composer import FakePorts, fixture

IMAGE_ID = "snapshot-1.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fixture-image-payload" * 8


class OwnerImageUpload:
    def __init__(self, root, owner="alice", data=PNG_BYTES):
        self.root = root
        self.owner = owner
        self.data = data
        self.path = root / IMAGE_ID
        self.path.write_bytes(data)

    def resolve_upload(self, att_id, *, owner):
        if att_id != IMAGE_ID or owner != self.owner:
            return None
        return {
            "id": att_id, "name": "snapshot.png", "mime": "image/png",
            "path": str(self.path), "owner": self.owner,
            "size": len(self.data), "checksum_sha256": hashlib.sha256(self.data).hexdigest(),
        }

    def _inside_upload_dir(self, path):
        return os.path.commonpath((str(self.root), os.path.realpath(path))) == str(self.root)


class ImagePorts(FakePorts):
    """Mimic the inherited multimodal preprocessing contract."""

    def __init__(self, handler_result=None):
        super().__init__()
        self.handler_result = handler_result

    async def preprocess_message(
        self, handler, message, ids, session, *,
        auto_opened_docs, allow_tool_preprocessing,
        allow_attachment_preprocessing=False,
    ):
        self.operations.append(("preprocess", allow_tool_preprocessing))
        if self.handler_result is not None:
            return self.handler_result
        meta = []
        for att_id in ids:
            info = handler.upload_handler.resolve_upload(att_id, owner=session.owner)
            if info:
                meta.append({
                    "id": info["id"], "name": info["name"], "mime": info["mime"],
                    "size": info["size"], "checksum_sha256": info["checksum_sha256"],
                })
        parts = [{"type": "text", "text": message}]
        for entry in meta:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{entry['mime']};base64,{entry['checksum_sha256']}"},
            })
        if not any(entry["mime"].startswith("image/") for entry in meta):
            # The inherited handler returns plain text when no image remains,
            # with bounded text upload bodies folded after the user request.
            text_bodies = []
            for entry in meta:
                info = handler.upload_handler.resolve_upload(
                    entry["id"], owner=session.owner,
                )
                if info and info.get("mime") in {
                    "text/plain", "text/markdown", "text/csv",
                    "application/json", "application/octet-stream",
                }:
                    with open(info["path"], "r", encoding="utf-8") as handle:
                        text_bodies.append(handle.read(65536))
            return PreprocessedMessage(
                message, message + "\n".join(text_bodies), message, [], meta,
            )
        return PreprocessedMessage(message, parts, message, [], meta)

    def upload_manifest(self, ids, handler, owner):
        self.operations.append(("manifest", owner))
        files = []
        for att_id in ids:
            info = handler.resolve_upload(att_id, owner=owner)
            if info is None:
                continue
            files.append({**info, "read_policy": "owner_checked_upload"})
        return files


def image_fixture(tmp_path, *, ports=None, persistence=None, **flags):
    admission, session, _ports, kwargs = fixture()
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    handler = SimpleNamespace(upload_handler=OwnerImageUpload(upload_root))
    settings_kwargs = {
        "attachment_profile": "text_and_image",
        "allow_tool_preprocessing": True,
    }
    settings_kwargs.update(flags)
    kwargs.update({
        "chat_handler": handler,
        "ports": ports if ports is not None else ImagePorts(),
        "request": TextTurnRequest("What is in this picture?", (IMAGE_ID,)),
        "settings": TrustedTextTurnSettings(**settings_kwargs),
    })
    if persistence is not None:
        kwargs["persistence"] = persistence
    return admission, session, kwargs


async def collect(admission, session, kwargs):
    return GatewayTurns(TurnService()).open_composed_text_events(
        admission, session, **kwargs,
    )


async def answer_model(_endpoint, _model, _messages, **_options):
    yield 'data: {"delta":"It is a fixture"}\n\n'
    yield "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_image_upload_reaches_model_as_multimodal_content(monkeypatch, tmp_path):
    admission, session, kwargs = image_fixture(tmp_path)
    received = {}

    async def model(_url, _model, messages, **options):
        received["messages"] = messages
        yield 'data: {"delta":"It is a fixture"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(turn_module, "stream_agent_loop", model)
    delivery = await collect(admission, session, kwargs)
    [event async for event in delivery.events]
    assert delivery.outcome.state == "completed"
    user_messages = [
        item for item in received["messages"]
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    assert user_messages[-1]["content"] == [
        {"type": "text", "text": kwargs["request"].message},
        {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{hashlib.sha256(PNG_BYTES).hexdigest()}",
        }},
    ]
    assert not session.messages


@pytest.mark.asyncio
async def test_image_profile_requires_enabled_preprocessing(monkeypatch, tmp_path):
    admission, session, kwargs = image_fixture(
        tmp_path, allow_tool_preprocessing=False,
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    with pytest.raises(AdmissionDenied, match="preprocessing"):
        [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_image_profile_rejects_persisted_turns(monkeypatch, tmp_path):
    from src.gateway_text_persistence import TrustedTextPersistence
    admission, session, kwargs = image_fixture(
        tmp_path,
        persistence=TrustedTextPersistence(
            session_manager=object(),
            saver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no saves")),
        ),
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    with pytest.raises(AdmissionDenied, match="attachment"):
        [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_invalid_attachment_profile_is_rejected(monkeypatch, tmp_path):
    admission, session, kwargs = image_fixture(tmp_path, attachment_profile="image")
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    with pytest.raises(AdmissionDenied, match="attachment profile"):
        [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_image_attachments_require_a_read_only_turn(monkeypatch, tmp_path):
    admission, session, kwargs = image_fixture(
        tmp_path, offered_tools=frozenset({"manage_notes"}),
    )
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    with pytest.raises(AdmissionDenied, match="read-only"):
        [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_oversized_image_is_rejected_before_context_io(monkeypatch, tmp_path):
    from src.gateway_attachment_context import resolve_admitted_attachments
    admission, session, kwargs = image_fixture(tmp_path)
    monkeypatch.setattr(
        "src.gateway_turn_composer.get_chat_upload_max_bytes",
        lambda: 8,
    )
    upload_handler = kwargs["chat_handler"].upload_handler
    with pytest.raises(AdmissionDenied, match="unavailable"):
        resolve_admitted_attachments(
            attachment_ids=(IMAGE_ID,), owner="alice",
            upload_handler=upload_handler, ports=kwargs["ports"],
            profile="text_and_image", max_chat_upload_bytes=8,
        )
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    with pytest.raises(AdmissionDenied, match="unavailable"):
        [event async for event in delivery.events]


@pytest.mark.asyncio
async def test_swapped_image_bytes_are_rejected_by_identity_check(monkeypatch, tmp_path):
    class StaleIndexUpload(OwnerImageUpload):
        def __init__(self, root, owner="alice"):
            super().__init__(root, owner=owner)
            self.stale_checksum = None
            self.stale_size = None

        def resolve_upload(self, att_id, *, owner):
            info = super().resolve_upload(att_id, owner=owner)
            if info and self.stale_checksum is not None:
                # Simulate an outdated upload index pointing at old bytes.
                info["checksum_sha256"] = self.stale_checksum
                info["size"] = self.stale_size
            return info

    admission, session, kwargs = image_fixture(tmp_path)
    stale_uploader = StaleIndexUpload(tmp_path / "uploads")
    stale_uploader.stale_checksum = "a" * 64
    stale_uploader.stale_size = len(PNG_BYTES)
    kwargs["chat_handler"] = SimpleNamespace(upload_handler=stale_uploader)
    kwargs["ports"] = ImagePorts()
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    # The manifest/resolve identity check rejects the stale snapshot before
    # any model call or conversation write.
    with pytest.raises(AdmissionDenied, match="unavailable"):
        [event async for event in delivery.events]
    assert not session.messages


@pytest.mark.asyncio
async def test_text_upload_still_admitted_in_image_profile(monkeypatch, tmp_path):
    text_id = "note-1.txt"
    admission, session, kwargs = image_fixture(tmp_path)
    root = tmp_path / "uploads"
    (root / text_id).write_text("hello", encoding="utf-8")

    class MixedUpload(OwnerImageUpload):
        def resolve_upload(self, att_id, *, owner):
            if att_id == text_id:
                data = (root / text_id).read_bytes()
                return {
                    "id": att_id, "name": "note.txt", "mime": "text/plain",
                    "path": str(root / text_id), "owner": owner,
                    "size": len(data), "checksum_sha256": hashlib.sha256(data).hexdigest(),
                }
            return super().resolve_upload(att_id, owner=owner)

    handler = SimpleNamespace(upload_handler=MixedUpload(root))
    kwargs["chat_handler"] = handler
    kwargs["request"] = TextTurnRequest("Summarize the note.", (text_id,))
    monkeypatch.setattr(turn_module, "stream_agent_loop", answer_model)
    delivery = await collect(admission, session, kwargs)
    [event async for event in delivery.events]
    assert delivery.outcome.state == "completed"
    assert not session.messages
