import asyncio
import json

from src.agent_loop import _resolved_tool_call_id
from src.gateway_turn_events import TurnOutcome, translate_legacy_turn_events
from src.gateway_web_compat_events import serialize_plain_text_turn_event


def test_browser_client_frames_keep_distinct_tool_ids_and_exit_codes():
    """Web may render trusted SSE frames with IDs; typed Core strips them."""
    async def legacy():
        for kind, command, code in (
            ("tool_start", "pwd", None),
            ("tool_start", "git status", None),
            ("tool_output", "git status", 128),
            ("tool_output", "pwd", 0),
        ):
            payload = {
                "type": kind, "tool": "bash", "command": command,
                "call_id": "call-pwd" if command == "pwd" else "call-git",
                "tool_call_id": "call-pwd" if command == "pwd" else "call-git",
                "exit_code": code,
            }
            yield "data: " + json.dumps(payload) + "\n\n"
        yield "data: [DONE]\n\n"

    async def collect():
        outcome = TurnOutcome()
        events = [
            item async for item in translate_legacy_turn_events(
                legacy(), outcome, client_rendering=True,
            )
        ]
        return events, outcome

    events, outcome = asyncio.run(collect())
    frames = [event for event in events if event.type == "client.frame"]
    assert len(frames) == 4
    assert [event.payload["call_id"] for event in frames] == [
        "call-pwd", "call-git", "call-git", "call-pwd",
    ]
    assert [event.payload["exit_code"] for event in frames] == [
        None, None, 128, 0,
    ]
    assert all(
        json.loads(serialize_plain_text_turn_event(event)[0][6:])["call_id"]
        == event.payload["call_id"]
        for event in frames
    )
    assert outcome.state == "completed"



def test_preserves_native_model_tool_call_id():
    assert _resolved_tool_call_id(
        {"id": "chatcmpl-tool-exact"},
        session_id="session-a",
        round_num=2,
        tool_index=1,
        tool_name="write_file",
    ) == "chatcmpl-tool-exact"


def test_generates_stable_id_for_harness_follow_through_call():
    kwargs = {
        "session_id": "session-a",
        "round_num": 2,
        "tool_index": 1,
        "tool_name": "private_browser",
    }

    first = _resolved_tool_call_id({}, **kwargs)
    second = _resolved_tool_call_id(None, **kwargs)

    assert first == second
    assert first.startswith("odysseus-auto-")


def test_generated_ids_distinguish_tool_positions():
    first = _resolved_tool_call_id(
        None,
        session_id="session-a",
        round_num=2,
        tool_index=1,
        tool_name="inspect_media",
    )
    second = _resolved_tool_call_id(
        None,
        session_id="session-a",
        round_num=2,
        tool_index=2,
        tool_name="inspect_media",
    )

    assert first != second
