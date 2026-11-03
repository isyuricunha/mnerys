"""M2.1 admitted pre-contract capability selection and inheritance parity."""

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gateway_admission import (
    AdmittedSession,
    AdmissionDenied,
    GatewayAdmission,
    Principal,
    SessionOwnership,
)
from src.gateway_turn_capabilities import (
    AdmittedTurnCapabilities,
    classify_admitted_turn_capabilities,
)
from src.gateway_turns import GatewayTurns
from src.turn_contract import corrected_browser_target, requested_capabilities


def issued(owner="alice", credential="browser"):
    principal = Principal(
        None if credential == "single_user" else owner,
        credential,
        frozenset({"chat"}) if credential == "bearer" else frozenset(),
    )
    return GatewayAdmission().admit(
        principal, "s1", lambda _: SessionOwnership(owner_id=owner),
    )


def call_args(
    *, owner="alice", credential="browser", message="Hello", history=None,
    use_turn_contract=True, active_document=False, workspace_bound=False,
    image_attachment=False, explicit_browser_intent=False,
    external_discovery_intent=False, private_browser_warm=False,
    contextual_browser_followup=False,
):
    admission = issued(owner, credential)
    session = SimpleNamespace(
        id="s1", owner=admission.principal.owner_id,
        history=list(history or []),
    )
    predicate = MagicMock(return_value=contextual_browser_followup)
    kwargs = dict(
        context_owner=admission.principal.owner_id,
        message=message,
        use_turn_contract=use_turn_contract,
        active_document=active_document,
        workspace_bound=workspace_bound,
        image_attachment=image_attachment,
        explicit_browser_intent=explicit_browser_intent,
        external_discovery_intent=external_discovery_intent,
        private_browser_warm=private_browser_warm,
        contextual_browser_followup_lookup=predicate,
    )
    return admission, session, kwargs


def inherited_route_reference(session, *, capabilities=None, **kwargs):
    """Test-only reproduction of the original Web classification sequence."""
    message = kwargs["message"]
    history = session.history or []
    browser_target = corrected_browser_target(message, history)
    base = (
        requested_capabilities(
            message, history,
            active_document=kwargs["active_document"],
            workspace=kwargs["workspace_bound"],
            image_attachment=kwargs["image_attachment"],
        )
        if kwargs["use_turn_contract"] else frozenset()
    )
    image_editing_requested = "image_editing" in base
    routed = base
    if kwargs["use_turn_contract"] and kwargs["explicit_browser_intent"]:
        routed = routed | {"search_browser"}
    if kwargs["use_turn_contract"] and kwargs["external_discovery_intent"]:
        routed = routed | {"search_browser"}
    if (
        kwargs["use_turn_contract"]
        and not routed
        and kwargs["private_browser_warm"]
        and kwargs["contextual_browser_followup_lookup"]()
    ):
        routed = frozenset({"search_browser"})
    active = routed
    if kwargs["use_turn_contract"] and kwargs["active_document"]:
        routed = routed | {"documents"}
    return (
        frozenset(routed),
        frozenset(active),
        browser_target,
        image_editing_requested,
        bool(
            kwargs["use_turn_contract"]
            and routed
            and "search_browser" not in routed
        ),
    )


@pytest.mark.parametrize("options", [
    {},
    {"message": "Create a note"},
    {"message": "Search the web for today's headlines"},
    {"message": "Check my calendar tomorrow"},
    {"message": "Hello", "active_document": True},
    {"message": "Check my calendar tomorrow", "active_document": True},
    {"message": "Hello", "workspace_bound": True},
    {"message": "Hello", "image_attachment": True},
    {"message": "Hello", "explicit_browser_intent": True},
    {"message": "Check my calendar", "explicit_browser_intent": True},
    {"message": "Hello", "external_discovery_intent": True},
    {"message": "Check my calendar", "external_discovery_intent": True},
    {
        "message": "Hello", "private_browser_warm": True,
        "contextual_browser_followup": True,
    },
    {
        "message": "Check my calendar", "private_browser_warm": True,
        "contextual_browser_followup": True,
    },
    {
        "message": "Hello", "private_browser_warm": True,
        "contextual_browser_followup": False,
    },
    {
        "message": "Hello", "private_browser_warm": False,
        "contextual_browser_followup": True,
    },
    {"message": "Create a note", "use_turn_contract": False},
    {
        "message": "Hello", "active_document": True,
        "use_turn_contract": False, "explicit_browser_intent": True,
    },
    {
        "message": "example.net",
        "history": [{"role": "user", "content": "Browse https://example.com"}],
    },
    {
        "message": "example.net", "use_turn_contract": False,
        "history": [{"role": "user", "content": "Browse https://example.com"}],
    },
    {
        "message": "Hello", "active_document": True,
        "private_browser_warm": True, "contextual_browser_followup": True,
    },
])
def test_matches_original_web_capability_order(options):
    grant, sess, params = call_args(**options)
    reference_predicate = MagicMock(
        return_value=options.get("contextual_browser_followup", False),
    )
    expected = inherited_route_reference(
        sess,
        **{**params, "contextual_browser_followup_lookup": reference_predicate},
    )
    result = classify_admitted_turn_capabilities(grant, sess, **params)
    assert isinstance(result, AdmittedTurnCapabilities)
    assert (
        result.capabilities,
        result.active_capabilities,
        result.corrected_browser_target,
        result.image_editing_requested,
        result.suppress_explicit_web_intent,
    ) == expected
    assert (
        params["contextual_browser_followup_lookup"].call_count
        == reference_predicate.call_count
    )


@pytest.mark.parametrize("credential", ["browser", "bearer", "single_user"])
def test_all_admitted_credential_projections_share_the_service(credential):
    grant, sess, params = call_args(
        credential=credential,
        message="Create a note",
    )
    direct = classify_admitted_turn_capabilities(grant, sess, **params)
    facade = GatewayTurns(MagicMock()).classify_turn_capabilities(
        grant, sess, **params,
    )
    assert facade == direct
    assert direct.capabilities == frozenset({"notes"})


def test_browser_capability_only_inherits_when_other_families_are_absent(monkeypatch):
    from src import gateway_turn_capabilities as service

    baseline = MagicMock(return_value=frozenset({"notes"}))
    monkeypatch.setattr(service, "requested_capabilities", baseline)
    grant, sess, kwargs = call_args(
        message="Continue", private_browser_warm=True,
        contextual_browser_followup=True,
    )
    result = service.classify_admitted_turn_capabilities(grant, sess, **kwargs)
    assert result.capabilities == frozenset({"notes"})
    kwargs["contextual_browser_followup_lookup"].assert_not_called()


def test_editor_extra_does_not_become_a_required_active_capability(monkeypatch):
    from src import gateway_turn_capabilities as service

    monkeypatch.setattr(
        service, "requested_capabilities", lambda *_args, **_kwargs: frozenset({"calendar"}),
    )
    grant, sess, kwargs = call_args(
        active_document=True, message="Check calendar",
    )
    result = service.classify_admitted_turn_capabilities(grant, sess, **kwargs)
    assert result.capabilities == frozenset({"calendar", "documents"})
    assert result.active_capabilities == frozenset({"calendar"})
    assert result.suppress_explicit_web_intent


def test_image_edit_signal_comes_from_pre_augmentation_classifier(monkeypatch):
    from src import gateway_turn_capabilities as service

    classify = MagicMock(return_value=frozenset({"image_editing"}))
    monkeypatch.setattr(service, "requested_capabilities", classify)
    grant, sess, kwargs = call_args(
        message="Edit this image",
        image_attachment=True,
        active_document=True,
        external_discovery_intent=True,
    )
    result = service.classify_admitted_turn_capabilities(grant, sess, **kwargs)
    classify.assert_called_once_with(
        "Edit this image", sess.history,
        active_document=True, workspace=False, image_attachment=True,
    )
    assert result.image_editing_requested
    assert result.capabilities == frozenset({"image_editing", "search_browser", "documents"})
    assert result.active_capabilities == frozenset({"image_editing", "search_browser"})
    assert not result.suppress_explicit_web_intent


def test_correction_uses_loaded_history_even_without_turn_contract():
    grant, sess, kwargs = call_args(
        message="example.net",
        use_turn_contract=False,
        history=[{"role": "user", "content": "Browse https://example.com"}],
    )
    result = classify_admitted_turn_capabilities(grant, sess, **kwargs)
    assert result.corrected_browser_target == {
        "url": "https://example.net",
        "objective": "Browse https://example.com",
    }
    assert result.capabilities == frozenset()
    assert result.active_capabilities == frozenset()


@pytest.mark.parametrize("tamper", [
    "missing", "fabricated", "proof", "session_id",
    "principal", "loaded_session", "loaded_owner", "context_owner",
])
def test_tampered_admission_denied_before_request_classification(monkeypatch, tamper):
    from src import gateway_turn_capabilities as service

    classify = MagicMock(side_effect=AssertionError("classifier invoked"))
    correction = MagicMock(side_effect=AssertionError("correction invoked"))
    monkeypatch.setattr(service, "requested_capabilities", classify)
    monkeypatch.setattr(service, "corrected_browser_target", correction)
    grant, sess, kwargs = call_args()
    if tamper == "missing":
        grant = None
    elif tamper == "fabricated":
        grant = AdmittedSession(Principal("alice", "browser"), "s1")
    elif tamper == "proof":
        grant = replace(grant, _proof=None)
    elif tamper == "session_id":
        grant = replace(grant, session_id="foreign")
    elif tamper == "principal":
        grant = replace(grant, principal=Principal("bob", "browser"))
    elif tamper == "loaded_session":
        sess.id = "foreign"
    elif tamper == "loaded_owner":
        sess.owner = "bob"
    elif tamper == "context_owner":
        kwargs["context_owner"] = "bob"

    with pytest.raises(AdmissionDenied) as denied:
        service.classify_admitted_turn_capabilities(grant, sess, **kwargs)
    assert denied.value.status_code == 403
    classify.assert_not_called()
    correction.assert_not_called()
    kwargs["contextual_browser_followup_lookup"].assert_not_called()


@pytest.mark.parametrize("override", [
    {"message": None},
    {"message": 42},
    {"use_turn_contract": 1},
    {"active_document": None},
    {"workspace_bound": "true"},
    {"image_attachment": 1},
    {"explicit_browser_intent": "yes"},
    {"external_discovery_intent": None},
    {"private_browser_warm": "true"},
    {"contextual_browser_followup_lookup": True},
])
def test_invalid_trusted_flags_deny_before_classification(monkeypatch, override):
    from src import gateway_turn_capabilities as service

    classify = MagicMock(side_effect=AssertionError("classifier invoked"))
    monkeypatch.setattr(service, "requested_capabilities", classify)
    grant, sess, kwargs = call_args()
    kwargs.update(override)
    with pytest.raises(AdmissionDenied):
        service.classify_admitted_turn_capabilities(grant, sess, **kwargs)
    classify.assert_not_called()


def test_invalid_browser_evidence_denied_instead_of_interpreted_as_consent(monkeypatch):
    from src import gateway_turn_capabilities as service

    monkeypatch.setattr(service, "requested_capabilities", lambda *_args, **_kw: frozenset())
    grant, sess, kwargs = call_args(private_browser_warm=True)
    kwargs["contextual_browser_followup_lookup"].return_value = "yes"
    with pytest.raises(AdmissionDenied):
        service.classify_admitted_turn_capabilities(grant, sess, **kwargs)


def test_incompatible_resolved_capabilities_denied(monkeypatch):
    from src import gateway_turn_capabilities as service

    monkeypatch.setattr(
        service, "requested_capabilities", lambda *_args, **_kw: frozenset({123}),
    )
    grant, sess, kwargs = call_args()
    with pytest.raises(AdmissionDenied):
        service.classify_admitted_turn_capabilities(grant, sess, **kwargs)


def test_capability_service_has_no_web_or_transport_imports():
    from src import gateway_turn_capabilities

    tree = ast.parse(inspect.getsource(gateway_turn_capabilities))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("routes", "fastapi", "starlette"))
        if isinstance(node, ast.Import):
            assert all(
                not name.name.startswith(("routes", "fastapi", "starlette"))
                for name in node.names
            )


def test_web_handoff_is_single_and_stays_before_policy_and_image_context():
    source = (
        Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway_turns"
        and node.func.attr == "classify_turn_capabilities"
    ]
    assert len(calls) == 1
    kw = {item.arg: item.value for item in calls[0].keywords}
    assert isinstance(kw["context_owner"], ast.Attribute)
    assert kw["context_owner"].attr == "user"
    assert source.index("gateway_turns.select_contract_scope(") < source.index(
        "gateway_turns.classify_turn_capabilities("
    ) < source.index("image_edit_context =")
    assert source.index("gateway_turns.classify_turn_capabilities(") < source.index(
        "gateway_turns.finalize_tool_policy("
    )
    assert "requested_capabilities(" not in source
    assert "corrected_browser_target(" not in source
    assert "_warm_turn_capabilities =" not in source
