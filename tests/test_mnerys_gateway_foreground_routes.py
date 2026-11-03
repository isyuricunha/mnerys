"""M2.1 owner-bound model/fallback routing for both Chat and streaming turns.

The existing resolver remains the source of truth for per-owner preferences,
allowed models and endpoint metadata. These tests do not invoke providers.
"""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from src import foreground_model_routing as routing
from src.foreground_model_routing import ForegroundModelPolicy
from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
)
from src.gateway_foreground_routes import AdmittedForegroundPolicy
from src.gateway_turns import GatewayTurns
from src.session_repository import SessionOwnership


PRIMARY = "https://primary.example.com/v1"
FALLBACK = "https://fallback.example.com/v1"


def admit(owner="alice", session="session-a", kind="browser"):
    scopes = frozenset({"chat"}) if kind == "bearer" else frozenset()
    return GatewayAdmission().admit(
        Principal(owner, kind, scopes),
        session,
        lambda _: SessionOwnership(owner),
    )


def gateway():
    return GatewayTurns(Mock())


def routes(gate, admission, selected, *, endpoint_id=None):
    return gate.build_foreground_routes(
        admission, selected,
        endpoint_url=PRIMARY,
        model="primary-model",
        headers={"Authorization": "REDACTED"},
        selected_endpoint_id=endpoint_id,
    )


def test_policy_uses_admitted_owner_and_server_allowed_models(monkeypatch):
    seen = []

    def fake_resolve(owner=None, allowed_models=None):
        seen.append((owner, allowed_models))
        return ForegroundModelPolicy(enabled=owner == "alice")

    monkeypatch.setattr(routing, "resolve_foreground_model_policy", fake_resolve)
    gate = gateway()
    admission = admit(owner="alice")
    prepared = gate.resolve_foreground_policy(
        admission, allowed_models={"primary-model", "fallback-model"},
    )
    assert prepared.enabled is True
    assert prepared.admission is admission
    assert seen == [("alice", {"primary-model", "fallback-model"})]

    bob = gate.resolve_foreground_policy(
        admit(owner="bob"), allowed_models={"primary-model"},
    )
    assert bob.enabled is False
    assert seen[-1] == ("bob", {"primary-model"})


@pytest.mark.parametrize("owner,kind", [
    ("alice", "browser"),
    ("alice", "bearer"),
    (None, "single_user"),
])
def test_supported_trusted_principals_resolve_their_own_policy(
    monkeypatch, owner, kind,
):
    captured = []
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda owner=None, allowed_models=None: (
            captured.append(owner) or ForegroundModelPolicy()
        ),
    )
    admission = admit(owner=owner, kind=kind)
    assert not gateway().resolve_foreground_policy(
        admission, allowed_models=None,
    ).enabled
    assert captured == [owner]


def test_strict_mode_keeps_only_selected_primary_and_safe_descriptor(monkeypatch):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_kw: ForegroundModelPolicy(),
    )
    monkeypatch.setattr(
        routing, "resolve_route_descriptor",
        lambda url, model, headers, owner: {
            "endpoint_id": "selected",
            "endpoint_label": "Selected route",
            "endpoint_cost_tracked": False,
        },
    )
    gate = gateway()
    admission = admit()
    policy = gate.resolve_foreground_policy(admission, allowed_models=None)
    candidates, descriptors = routes(gate, admission, policy)
    assert candidates == [(PRIMARY, "primary-model", {"Authorization": "REDACTED"})]
    assert descriptors == [{
        "endpoint_id": "selected",
        "endpoint_label": "Selected route",
        "endpoint_cost_tracked": False,
    }]
    assert "Authorization" not in str(descriptors)


def test_fallback_candidates_and_metadata_stay_aligned(monkeypatch):
    fallback = (FALLBACK, "fallback-model", {"Authorization": "REDACTED"})
    policy = ForegroundModelPolicy(
        enabled=True,
        fallback_candidates=(fallback, fallback),  # dedup remains unchanged
        fallback_descriptors=(
            {"endpoint_id": "f1", "endpoint_label": "Fallback 1"},
            {"endpoint_id": "f2", "endpoint_label": "Fallback 2"},
        ),
    )
    monkeypatch.setattr(routing, "resolve_foreground_model_policy", lambda **_: policy)
    seen = []
    monkeypatch.setattr(
        routing, "resolve_route_descriptor_by_id",
        lambda endpoint_id, url, model, headers, owner: (
            seen.append((endpoint_id, owner))
            or {
                "endpoint_id": endpoint_id,
                "endpoint_label": "My selected endpoint",
                "endpoint_cost_tracked": False,
            }
        ),
    )

    gate = gateway()
    admission = admit("alice")
    prepared = gate.resolve_foreground_policy(admission, allowed_models=None)
    candidates, descriptors = routes(
        gate, admission, prepared, endpoint_id="selected-a",
    )
    assert seen == [("selected-a", "alice")]
    assert candidates == [
        (PRIMARY, "primary-model", {"Authorization": "REDACTED"}),
        fallback,
    ]
    assert [d["endpoint_id"] for d in descriptors] == ["selected-a", "f1"]
    assert all("Authorization" not in str(d) for d in descriptors)


def test_unresolved_explicit_endpoint_id_is_not_relabelled(monkeypatch):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(),
    )
    monkeypatch.setattr(routing, "resolve_route_descriptor_by_id", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        routing, "resolve_route_descriptor",
        lambda *_a, **_kw: pytest.fail("Never fall back to another account"),
    )
    admission = admit()
    gate = gateway()
    prepared = gate.resolve_foreground_policy(admission, allowed_models=None)
    _, descriptors = routes(gate, admission, prepared, endpoint_id="foreign-id")
    assert descriptors[0]["endpoint_id"] is None
    assert descriptors[0]["endpoint_label"] == "Selected route"


@pytest.mark.parametrize("tamper", ["missing", "fabricated", "wrong_owner", "wrong_session"])
def test_foreground_policy_cannot_be_resolved_without_issued_admission(
    monkeypatch, tamper,
):
    original = admit()
    invalid = {
        "missing": None,
        "fabricated": AdmittedSession(original.principal, original.session_id),
        "wrong_owner": replace(original, principal=Principal("mallory", "browser")),
        "wrong_session": replace(original, session_id="other-session"),
    }[tamper]
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_kw: pytest.fail("Unadmitted principal reached user preferences"),
    )
    with pytest.raises(AdmissionDenied):
        gateway().resolve_foreground_policy(invalid, allowed_models=None)


@pytest.mark.parametrize("tamper", ["missing", "fabricated", "other_session", "other_owner", "other_policy"])
def test_foreground_routes_refuse_reused_or_fabricated_admission(
    monkeypatch, tamper,
):
    monkeypatch.setattr(
        routing, "resolve_foreground_model_policy",
        lambda **_: ForegroundModelPolicy(),
    )
    monkeypatch.setattr(
        routing, "build_foreground_model_candidates",
        lambda *_a, **_kw: pytest.fail("Unauthorized route resolution"),
    )
    gate = gateway()
    original = admit()
    selected = gate.resolve_foreground_policy(original, allowed_models=None)
    other = {
        "missing": None,
        "fabricated": AdmittedSession(original.principal, original.session_id),
        "other_session": admit(session="different"),
        "other_owner": admit(owner="bob"),
        "other_policy": original,
    }[tamper]
    if tamper == "other_policy":
        selected = AdmittedForegroundPolicy(original, ForegroundModelPolicy())
    with pytest.raises(AdmissionDenied) as error:
        routes(gate, other, selected)
    assert error.value.status_code == 403


def test_mismatched_candidate_metadata_fails_instead_of_silent_mislabel(
    monkeypatch,
):
    monkeypatch.setattr(routing, "resolve_foreground_model_policy", lambda **_: ForegroundModelPolicy())
    monkeypatch.setattr(
        routing, "build_foreground_model_candidates",
        lambda *_a, **_kw: [(PRIMARY, "primary-model", {})],
    )
    monkeypatch.setattr(
        routing, "build_foreground_route_descriptors",
        lambda *_a, **_kw: [],
    )
    admission = admit()
    gate = gateway()
    prepared = gate.resolve_foreground_policy(admission, allowed_models=None)
    with pytest.raises(RuntimeError, match="descriptor mismatch"):
        routes(gate, admission, prepared)
