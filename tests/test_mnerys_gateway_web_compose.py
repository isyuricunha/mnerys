"""Transport-neutral full Web turn composition (prepare stage).

Parity and security tests for the admitted Gateway composition that the
production Web route uses instead of orchestrating policy itself. No HTTP
app, live model or real database is used.
"""

from types import SimpleNamespace

import pytest

from src.gateway_admission import AdmissionDenied, AdmittedSession, Principal
from src.gateway_turns import GatewayTurns, WebTurnComposition
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action
from src.turn_service import TurnService
from tests.test_mnerys_gateway_composed_tools import configured
from tests.test_mnerys_gateway_turn_composer import FakePorts


class WebComposePorts(FakePorts):
    """Accepts the inherited user/private writes like the legacy Web ports."""

    def __init__(self):
        super().__init__()
        self.private_writes = []

    def append_user_message(
        self, session, _handler, preprocessed, *,
        incognito, interaction_mode, auto_escalated,
    ):
        assert incognito is False
        self.operations.append(("save_user", interaction_mode))
        session.messages.append({
            "role": "user", "content": preprocessed.user_content,
        })

    def append_private_message(self, session_id, role, content, metadata):
        self.operations.append(("private", session_id, role))
        self.private_writes.append({"role": role, "content": content})
        return {"role": role, "content": content}

    def private_messages(self, session_id):
        return list(self.private_writes)


@pytest.fixture()
def web_build(monkeypatch):
    admission, session, ports, _options = configured(monkeypatch)
    ports = WebComposePorts()

    def build(**overrides):
        values = {
            "admission": admission,
            "session": session,
            "session_id": session.id,
            "message": "List my notes",
            "chat_mode": "agent",
            "privileges": {},
            "global_disabled_tools": (),
            "browser_tools": frozenset(),
            "ports": ports,
            "chat_handler": SimpleNamespace(upload_handler=None),
            "chat_processor": SimpleNamespace(),
            "allowed_models": None,
        }
        values.update(overrides)
        return WebTurnComposition(**values), admission, session, ports

    return build, admission, session, ports


@pytest.mark.asyncio
async def test_web_agent_turn_prepares_inventory_and_contract(web_build):
    build, _admission, session, _ports = web_build
    composition, _a, _s, _p = build()
    prepared = await GatewayTurns(TurnService()).compose_web_turn(composition)
    assert prepared.context_owner == "alice"
    assert prepared.chat_mode == "agent"
    assert prepared.context is not None
    assert prepared.turn_contract is not None
    assert prepared.turn_contract.executable
    assert prepared.request_authority.bound_to(
        owner="alice", session_id=session.id,
    )
    # The inherited Web context port appended the user turn once.
    assert [item["role"] for item in session.messages] == ["user"]


@pytest.mark.asyncio
async def test_web_chat_turn_composes_bound_authority_without_contract(web_build):
    build, _admission, session, _ports = web_build
    composition, _a, _s, _p = build(chat_mode="chat")
    prepared = await GatewayTurns(TurnService()).compose_web_turn(composition)
    assert prepared.chat_mode == "chat"
    assert prepared.context is not None
    assert prepared.turn_contract is None
    assert prepared.request_authority.bound_to(
        owner="alice", session_id=session.id,
    )
    assert [item["role"] for item in session.messages] == ["user"]


@pytest.mark.asyncio
async def test_forged_admission_never_reaches_the_intent_chain(web_build):
    build, _admission, session, _ports = web_build
    forged = AdmittedSession(Principal("alice", "browser"), session.id)
    composition, _a, _s, _p = build(admission=forged)
    with pytest.raises(AdmissionDenied):
        await GatewayTurns(TurnService()).compose_web_turn(composition)


@pytest.mark.asyncio
async def test_foreign_owner_composition_is_rejected(web_build):
    build, _admission, session, _ports = web_build
    session.owner = "mallory"
    composition, _a, _s, _p = build()
    with pytest.raises(AdmissionDenied):
        await GatewayTurns(TurnService()).compose_web_turn(composition)


@pytest.mark.asyncio
async def test_denied_approval_short_circuits_before_context_io(web_build):
    build, _admission, session, _ports = web_build
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id=session.id, origin_run_id="web-deny-run",
        tool_name="manage_notes", content='{"action":"add"}', workspace=None,
        request_text="Add a note",
        continuation_query="Add a note",
        external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("manage_notes", '{"action":"add"}'),
        selected_tools={"manage_notes"},
        request_authority=None,
    )
    composition, _a, _s, _p = build(
        tool_approval_id=pending.approval_id,
        tool_approval_decision="deny",
        approval_store=store,
    )
    prepared = await GatewayTurns(TurnService()).compose_web_turn(composition)
    assert prepared.approval_denied is True
    assert prepared.context is None
    assert prepared.request_authority is None
    assert store.peek(pending.approval_id) is None


@pytest.mark.asyncio
async def test_incognito_composition_strips_identity_tools(web_build):
    build, _admission, session, _ports = web_build
    composition, _a, _s, _p = build(incognito=True)
    prepared = await GatewayTurns(TurnService()).compose_web_turn(composition)
    assert prepared.tool_policy.blocks("manage_memory")
    assert prepared.tool_policy.blocks("search_chats")
    assert prepared.tool_policy.blocks("manage_skills")


@pytest.mark.asyncio
async def test_active_email_composition_denies_compose_tools(web_build):
    build, _admission, session, _ports = web_build
    composition, _a, _s, _p = build(
        active_email={"uid": "7", "folder": "INBOX", "account": ""},
    )
    prepared = await GatewayTurns(TurnService()).compose_web_turn(composition)
    assert prepared.tool_policy.blocks("send_email")
    assert prepared.tool_policy.blocks("reply_to_email")
    assert prepared.tool_policy.blocks("create_document")


@pytest.mark.asyncio
async def test_composition_requires_context_ports(web_build):
    build, _admission, session, _ports = web_build
    composition, _a, _s, _p = build(ports=None)
    with pytest.raises(AdmissionDenied, match="ports"):
        await GatewayTurns(TurnService()).compose_web_turn(composition)
