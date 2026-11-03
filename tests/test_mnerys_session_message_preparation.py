"""M1 shared persisted-message preparation, reused by live legacy writers.

The codec does not issue authorization or change the legacy SQL schema. It is
the serialization seam shared with the *inactive* incarnation-fenced writer.
"""

import json

import pytest

from src.attachment_refs import persistable_message_content
from src.session_message_preparation import (
    PreparedSessionMessage, prepare_session_message,
)
from src.session_write_fencing import (
    PreparedSessionMessage as FencedPreparedSessionMessage,
)


def test_prepared_message_value_is_shared_with_fenced_writer():
    assert PreparedSessionMessage is FencedPreparedSessionMessage


def test_plain_message_matches_inherited_db_serialization():
    metadata = {"timestamp": "2026-01-01T00:00:00Z", "owner": "alice"}
    prepared = prepare_session_message("assistant", "plain text", metadata)

    assert prepared == PreparedSessionMessage(
        role="assistant",
        content="plain text",
        metadata_json=json.dumps(metadata),
    )
    assert json.loads(prepared.metadata_json) == metadata
    assert metadata == {"timestamp": "2026-01-01T00:00:00Z", "owner": "alice"}


@pytest.mark.parametrize("metadata", [None, {}, {"status": "ok", "diacritic": "café"}])
def test_optional_metadata_encoding_retains_inherited_json_wire_shape(metadata):
    before = None if metadata is None else dict(metadata)
    prepared = prepare_session_message("user", "hello", metadata)
    assert prepared.metadata_json == (json.dumps(metadata) if metadata else None)
    assert metadata == before


def test_multimodal_payload_collapses_to_text_and_attachment_reference():
    inline_media = "data:image/png;base64,QUJDRA=="
    content = [
        {"type": "text", "text": "See the attachment"},
        {"type": "image_url", "image_url": {"url": inline_media}},
    ]
    metadata = {
        "attachments": [
            {"id": "upload-001", "name": "example.png", "mime": "image/png"},
        ]
    }

    prepared = prepare_session_message("user", content, metadata)

    assert prepared.content == persistable_message_content(content, metadata)
    assert "See the attachment" in prepared.content
    assert "upload-001" in prepared.content
    assert "QUJDRA==" not in prepared.content
    assert metadata["attachments"][0]["id"] == "upload-001"
    assert content[1]["image_url"]["url"] == inline_media


def test_textual_inline_media_is_not_persisted_raw():
    prepared = prepare_session_message(
        "assistant", "Rendered: data:image/png;base64,QUJDRA==",
    )
    assert "QUJDRA==" not in prepared.content
    assert "[inline media omitted from persisted history]" in prepared.content


def test_storage_payload_preserves_json_conventions_for_structured_content():
    content = {"type": "unexpected-shape", "value": ["abc", 23]}
    prepared = prepare_session_message("tool", content)
    assert prepared.content == persistable_message_content(content)
    assert prepared.metadata_json is None


@pytest.mark.parametrize("bad_role", ["", "  ", None, 1])
def test_malformed_role_does_not_become_persisted_message(bad_role):
    with pytest.raises(ValueError):
        prepare_session_message(bad_role, "content")


def test_payload_is_frozen_not_a_mutable_authorization_handle():
    from dataclasses import FrozenInstanceError
    prepared = prepare_session_message("assistant", "hello")
    with pytest.raises(FrozenInstanceError):
        prepared.content = "modified"
