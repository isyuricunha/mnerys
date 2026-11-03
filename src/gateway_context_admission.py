"""Trusted session-owner projection for context construction in M2.1.

The Web adapter authenticates and performs the ownership lookup. The context
builder receives only this checked principal's owner; it must not read
cookies, HTTP headers or synthetic client-selected owner fields.
"""

from typing import Any

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)


def owner_for_admitted_context(
    admission: AdmittedSession,
    session: Any,
    requested_session_id: str,
) -> str | None:
    """Refuse context construction for forged or mismatched session objects."""
    session_id = require_admitted_session(admission)
    if (
        requested_session_id != session_id
        or getattr(session, "id", None) != session_id
    ):
        raise AdmissionDenied(403, "Context session does not match admission")

    owner = admission.principal.owner_id
    loaded_owner = getattr(session, "owner", None)
    if loaded_owner is not None and loaded_owner != owner:
        raise AdmissionDenied(403, "Context session owner does not match admission")
    return owner
