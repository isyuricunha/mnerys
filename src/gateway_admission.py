"""M1 internal Gateway principal and session admission policy.

Transport adapters MUST derive Principal from server-validated credentials.
User or model payloads must never supply Principal or AdmittedSession. This
module does not authenticate requests, issue tokens or implement durable turns.
"""

from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Literal, Optional


CredentialKind = Literal["browser", "bearer", "single_user"]


@dataclass(frozen=True)
class Principal:
    """Trusted identity projection; not a user-supplied request schema."""

    owner_id: Optional[str]
    credential_kind: CredentialKind
    scopes: FrozenSet[str] = frozenset()

    @property
    def delegated(self) -> bool:
        return self.credential_kind == "bearer"


@dataclass(frozen=True)
class SessionOwnership:
    """Existing session lookup result. None from lookup means not found."""

    owner_id: Optional[str]


class AdmissionDenied(Exception):
    """A failed admission, mapped to an HTTP response by the transport adapter."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_ADMISSION_PROOF = object()


@dataclass(frozen=True)
class AdmittedSession:
    """Internal evidence of a checked session; not a portable bearer token."""

    principal: Principal
    session_id: str
    _proof: object = field(default=None, repr=False, compare=False)


class GatewayAdmission:
    """Apply the same server-side session access policy for each transport.

    Lookup is injected so this policy has no FastAPI, SQLAlchemy, app startup
    or process-global database dependency. The current Web adapter provides
    the inherited DB lookup and optional same-process ghost-session fallback.
    """

    def admit(
        self,
        principal: Principal,
        session_id: str,
        lookup: Callable[[str], Optional[SessionOwnership]],
    ) -> AdmittedSession:
        if not isinstance(principal, Principal):
            raise AdmissionDenied(401, "Authentication required")

        if principal.credential_kind == "bearer":
            if "chat" not in principal.scopes or not _valid_owner(principal.owner_id):
                raise AdmissionDenied(403, "API token missing required chat authorization")
        elif principal.credential_kind == "browser":
            if not _valid_owner(principal.owner_id):
                raise AdmissionDenied(401, "Authentication required")
        elif principal.credential_kind == "single_user":
            if principal.owner_id is not None:
                raise AdmissionDenied(401, "Invalid single-user principal")
        else:
            raise AdmissionDenied(401, "Authentication required")

        if not isinstance(session_id, str) or not session_id.strip():
            raise AdmissionDenied(404, "Session not found")

        session = lookup(session_id)
        if session is None:
            raise AdmissionDenied(404, f"Session {session_id} not found")
        if not isinstance(session, SessionOwnership):
            raise AdmissionDenied(404, "Session not found")
        if (
            principal.owner_id is not None
            and session.owner_id != principal.owner_id
        ):
            # Do not reveal whether a foreign session exists.
            raise AdmissionDenied(404, f"Session {session_id} not found")

        return AdmittedSession(
            principal, session_id, (_ADMISSION_PROOF, principal, session_id)
        )


def _valid_owner(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def require_admitted_session(value: AdmittedSession) -> str:
    """Reject accidentally bypassing admission at the internal run facade."""
    proof = getattr(value, "_proof", None)
    if (
        not isinstance(value, AdmittedSession)
        or not isinstance(proof, tuple)
        or len(proof) != 3
        or proof[0] is not _ADMISSION_PROOF
        or proof[1] is not value.principal
        or proof[2] != value.session_id
        or not isinstance(value.session_id, str)
        or not value.session_id.strip()
    ):
        raise AdmissionDenied(403, "An admitted session is required")
    return value.session_id


gateway_admission = GatewayAdmission()
