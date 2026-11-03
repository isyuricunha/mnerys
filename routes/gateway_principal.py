"""FastAPI-to-Gateway identity adapter for existing authenticated chat routes.

Only read state assigned by the server's auth middleware. Request JSON, headers
and model output are never accepted as authority or as a Principal.
"""

from fastapi import Request

from src.auth_helpers import (
    _auth_disabled,
    effective_user,
    is_delegated_credential,
    require_api_token_scope,
)
from src.gateway_admission import Principal


def principal_for_chat_request(request: Request) -> Principal:
    """Project the existing validated browser/bearer identity into the Core.

    A bearer token must pass the existing scope guard before the session DB is
    queried, even if the authenticated token belongs to an administrator.
    """
    validated_owner = require_api_token_scope(request, "chat")
    if is_delegated_credential(request):
        return Principal(
            owner_id=validated_owner,
            credential_kind="bearer",
            scopes=frozenset(getattr(request.state, "api_token_scopes", ()) or ()),
        )

    owner = effective_user(request)
    if owner:
        return Principal(owner_id=owner, credential_kind="browser")
    if _auth_disabled():
        return Principal(owner_id=None, credential_kind="single_user")
    return Principal(owner_id=None, credential_kind="browser")
