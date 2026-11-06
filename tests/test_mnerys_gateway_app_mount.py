"""The real application mounts the Gateway API v1 on the shared control plane.

Source-level regression: the v1 router must be wired into app.py behind the
same singletons the Web cutover uses, so the CLI and the Web share one
authority. Importing app:app directly in CI is deliberately avoided; the
deployment runner and smoke tests boot it, and the live CLI acceptance
exercises the router over HTTP.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_SOURCE = (REPO / "app.py").read_text(encoding="utf-8")


def test_app_wires_the_gateway_api_into_the_shared_state():
    assert "gateway_api_router" in APP_SOURCE
    assert "app.include_router(gateway_api_router)" in APP_SOURCE
    assert "app.state.gateway_turns = gateway_turns" in APP_SOURCE
    assert "app.state.turn_service = turn_service" in APP_SOURCE
    assert "app.state.tool_approval_store = tool_approval_store" in APP_SOURCE
    assert "app.state.model_discovery = _gateway_model_snapshot" in APP_SOURCE
    assert "app.state.chat_handler = chat_handler" in APP_SOURCE
    assert "app.state.chat_processor = chat_processor" in APP_SOURCE
    assert "app.state.text_persistence = TrustedTextPersistence(" in APP_SOURCE
    assert "app.state.gateway_turn_records = SqlTurnRecords(" in APP_SOURCE
    # Names only, no schemas or arguments: discovery never confers rights.
    assert "app.state.tool_discovery = sorted(BUILTIN_TOOL_DESCRIPTIONS)" in APP_SOURCE


def test_model_snapshot_uses_enabled_endpoints_only():
    assert "ModelEndpoint.is_enabled.is_(True)" in APP_SOURCE