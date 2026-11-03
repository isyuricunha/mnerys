"""M2.1 admitted foreground model routing shared by Chat and streaming turns.

This module delegates to the existing owner-scoped preference and endpoint
resolvers. It does not select models from client-supplied authority, add a
provider, persist settings, or expose a public Gateway API.
"""

from dataclasses import dataclass, field
from typing import Any, Collection

from src import foreground_model_routing
from src.foreground_model_routing import ForegroundModelPolicy
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    require_admitted_session,
)

_ROUTE_PROOF = object()


@dataclass(frozen=True)
class AdmittedForegroundPolicy:
    """A turn's fallback preference bound to one issued admission."""

    admission: AdmittedSession
    policy: ForegroundModelPolicy
    _proof: object = field(default=None, repr=False, compare=False)

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    @property
    def eligible_statuses(self) -> frozenset[int]:
        return self.policy.eligible_statuses

    @property
    def fallback_on_empty(self) -> bool:
        return self.policy.fallback_on_empty


def resolve_admitted_foreground_policy(
    admission: AdmittedSession,
    *,
    allowed_models: Collection[str] | None,
) -> AdmittedForegroundPolicy:
    """Read fallback consent for the admitted owner and allowed model set."""
    require_admitted_session(admission)
    owner = admission.principal.owner_id
    policy = foreground_model_routing.resolve_foreground_model_policy(
        owner=owner, allowed_models=allowed_models,
    )
    return AdmittedForegroundPolicy(admission, policy, _ROUTE_PROOF)


def require_admitted_foreground_policy(
    admission: AdmittedSession,
    prepared: AdmittedForegroundPolicy,
) -> ForegroundModelPolicy:
    """Require the policy issued for this exact admitted turn."""
    require_admitted_session(admission)
    if (
        not isinstance(prepared, AdmittedForegroundPolicy)
        or prepared._proof is not _ROUTE_PROOF
        or prepared.admission is not admission
        or not isinstance(prepared.policy, ForegroundModelPolicy)
    ):
        raise AdmissionDenied(403, "Admitted foreground model policy required")
    return prepared.policy


def build_admitted_foreground_routes(
    admission: AdmittedSession,
    prepared: AdmittedForegroundPolicy,
    *,
    endpoint_url: str,
    model: str,
    headers: dict[str, Any] | None,
    selected_endpoint_id: str | None,
) -> tuple[list, list]:
    """Return the existing candidate/descriptor lists in matching order.

    The prepared preference cannot be moved between sessions or users, nor
    constructed from a request body. Provider credentials stay within the
    candidate tuples; route descriptors remain the inherited safe metadata.
    """
    policy = require_admitted_foreground_policy(admission, prepared)

    owner = admission.principal.owner_id
    candidates = foreground_model_routing.build_foreground_model_candidates(
        endpoint_url, model, headers,
        owner=owner, policy=policy,
    )
    descriptors = foreground_model_routing.build_foreground_route_descriptors(
        endpoint_url, model, headers,
        owner=owner, policy=policy,
        selected_endpoint_id=selected_endpoint_id,
    )
    if len(candidates) != len(descriptors):
        raise RuntimeError("Foreground model route descriptor mismatch")
    return candidates, descriptors
