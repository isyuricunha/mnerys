"""Fail-closed preflight for already sealed native filesystem approvals.

The authenticated, server-selected workspace remains the only supported root.
Never interpret a tool path or approval-button input as a filesystem grant.
The inherited dispatcher performs its own exact resource revalidation again
before an effect. These checks are not kernel-handle/transaction fencing.
"""

from src.agent_runtime.authority import ExactOperation, RequestAuthority
from src.agent_runtime.resource_binding import (
    BoundFilesystemOperation, NATIVE_FILESYSTEM_TOOLS,
    resolve_filesystem_operation,
)
from src.agent_runtime.resources import FilesystemScope
from src.gateway_admission import AdmissionDenied
from src.tool_approvals import PendingToolApproval


def require_sealed_filesystem_approval(
    pending: PendingToolApproval,
    *,
    owner: str,
    session_id: str,
    workspace: str | None,
) -> None:
    """Reject missing, expanded, replaced or foreign approved filesystem roots."""
    if (
        not isinstance(pending, PendingToolApproval)
        or pending.tool_name not in NATIVE_FILESYSTEM_TOOLS
        or not workspace
        or pending.workspace != workspace
        or pending.owner != owner
        or pending.session_id != session_id
        or pending.document_id
        or pending.document_version is not None
        or pending.document_digest
        or pending.owned_operation is not None
        or pending.process_operation is not None
        or pending.browser_operation is not None
    ):
        raise AdmissionDenied(409, "Unsupported filesystem approval context")

    bound = pending.resource_operation
    original = pending.request_authority
    if (
        not isinstance(bound, BoundFilesystemOperation)
        or not isinstance(original, RequestAuthority)
        or not original.bound_to(
            owner=owner, session_id=session_id, workspace=workspace,
        )
        or original.inherited
        or not bound.request_id
        or bound.request_id != original.request_id
        or not bound.bindings
    ):
        raise AdmissionDenied(409, "Filesystem approval has no originating resource binding")

    roots = tuple(dict.fromkeys(binding.resource.root for binding in bound.bindings))
    if (
        len(roots) != 1
        or roots[0].path != workspace
        or roots[0].owner != owner
        or roots[0].scope is not FilesystemScope.WORKSPACE
        or (
            original.resource_roots
            and roots[0] not in original.resource_roots
        )
    ):
        raise AdmissionDenied(409, "Filesystem approval exceeds the admitted workspace")

    try:
        operation = ExactOperation.normalize(pending.tool_name, pending.content)
        if bound.operation != operation:
            raise ValueError("Approved action differs from sealed resource action")
        # Use the original observed root, not a fresh FilesystemRoot.seal()
        # that might accidentally accept a replaced workspace.
        bound.validate()
        current = resolve_filesystem_operation(
            operation, roots=roots, workspace=workspace,
            request_id=original.request_id,
        )
        if current != bound:
            raise ValueError("Filesystem resource was retargeted")
    except Exception as exc:
        # Do not send arbitrary path names/SQL or OS diagnostics to clients.
        raise AdmissionDenied(409, "Filesystem resource identity changed") from exc
