"""Read-only, admitted active-document resolution for the direct Gateway.

The selected ID is untrusted even when routed through a trusted adapter.
This narrow profile requires an existing, active document owned by the exact
admitted user and *linked to the same session*. Unlike the existing Web editor,
it never rebinds a document from another session.

All ORM sessions are short-lived; no database handle crosses into the Agent
Loop. This is optimistic preflight, not an atomic document version fence.
"""

from dataclasses import dataclass
import re

from src.gateway_admission import AdmissionDenied
from src.tool_approvals import PendingToolApproval, document_content_digest


@dataclass(frozen=True)
class AdmittedActiveDocument:
    id: str
    owner: str
    session_id: str
    title: str
    language: str | None
    current_content: str
    version_count: int
    content_digest: str


def load_admitted_active_document(
    *, owner: str, session_id: str, document_id: str,
) -> AdmittedActiveDocument:
    """Verify direct selection against the existing owner-scoped SQL store.

    Deliberately disallows missing/legacy ownership and cross-session UI
    handoffs: those need an explicitly authorized Web adapter operation.
    """
    if (
        type(document_id) is not str or not document_id.strip()
        or len(document_id) > 256
        or any(ord(char) < 32 for char in document_id)
        or not isinstance(owner, str) or not owner
        or not isinstance(session_id, str) or not session_id
    ):
        raise AdmissionDenied(403, "Invalid active document selector")

    from core.database import SessionLocal, Document, Session as DBSession

    try:
        with SessionLocal() as db:
            doc = (
                db.query(Document)
                .join(DBSession, Document.session_id == DBSession.id)
                .filter(
                    DBSession.id == session_id,
                    DBSession.owner == owner,
                    Document.id == document_id,
                    Document.owner == owner,
                    Document.session_id == session_id,
                    Document.is_active.is_(True),
                    Document.archived.is_(False),
                )
                .first()
            )
            if (
                doc is None
                or type(doc.version_count) is not int
                or doc.version_count < 1
                or not isinstance(doc.current_content, str)
                or not isinstance(doc.title, str)
            ):
                raise AdmissionDenied(409, "Active document is missing or has changed")
            return AdmittedActiveDocument(
                id=doc.id,
                owner=doc.owner,
                session_id=doc.session_id,
                title=doc.title,
                language=doc.language,
                current_content=doc.current_content,
                version_count=doc.version_count,
                content_digest=document_content_digest(doc.current_content),
            )
    except AdmissionDenied:
        raise
    except Exception as exc:
        raise AdmissionDenied(409, "Unable to verify the active document") from exc


def require_sealed_document_snapshot(
    pending: PendingToolApproval,
    document: AdmittedActiveDocument | None,
) -> None:
    """Require the same previously sealed document and owned-operation ID."""
    from src.agent_runtime.owned_resources import BoundOwnedOperation

    owned = pending.owned_operation
    if (
        document is None
        or not isinstance(owned, BoundOwnedOperation)
        or not isinstance(pending.document_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", pending.document_digest or "")
        or pending.document_id != document.id
        or pending.document_version != document.version_count
        or pending.document_digest != document.content_digest
        or owned.document_id != document.id
        or owned.document_version != document.version_count
        or owned.document_digest != document.content_digest
        or owned.owner != pending.owner
        or owned.thread_id != pending.session_id
        or not any(
            resource.namespace == "documents"
            and resource.record_id == document.id
            and resource.owner == document.owner
            and resource.thread_id == document.session_id
            for resource in owned.resources
        )
    ):
        raise AdmissionDenied(409, "Document no longer matches its sealed approval")
    # The dispatcher repeats this check before the actual effect. It detects
    # record reincarnation/revision, not just version and text edits.
    try:
        owned.validate()
    except Exception as exc:
        # SQL backend failures also fail closed, without exposing database
        # targets or credentials through a direct-turn error.
        raise AdmissionDenied(409, "Document resource identity changed") from exc
