"""M5.2 increment 6: real-provider canonical tool acceptance.

Runs the canonical tool matrix through Mnerys' real Agent Core
(``stream_agent_loop`` -> Gateway admission -> authorized execution -> real
result -> model continuation) against configured provider endpoints. Two
protocol families are exercised when configured:

- OpenAI Chat Completions   (``MNERYS_LIVE_BASE_URL``,  e.g. https://host/v1)
- Anthropic Messages        (``MNERYS_LIVE_MESSAGES_URL``, e.g. https://host/v1/messages)

Credentials come ONLY from the environment and are never printed, logged or
written into artifacts. The run uses a disposable data directory
(``ODYSSEUS_DATA_DIR``) and a disposable workspace, so no production database
or user data is touched.

Usage:
  MNERYS_LIVE_ACCEPTANCE=1 \\
  MNERYS_LIVE_BASE_URL=https://host/v1 \\
  MNERYS_LIVE_MESSAGES_URL=https://host/v1/messages \\
  MNERYS_LIVE_MODEL=model-id \\
  MNERYS_LIVE_API_KEY=... \\
  python3 scripts/dev/live_provider_acceptance.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid

if os.environ.get("MNERYS_LIVE_ACCEPTANCE") != "1":
    print("SETUP-SKIP: live acceptance not requested")
    sys.exit(0)

_BASE_URL = os.environ.get("MNERYS_LIVE_BASE_URL", "").strip()
_MESSAGES_URL = os.environ.get("MNERYS_LIVE_MESSAGES_URL", "").strip()
_MODEL = os.environ.get("MNERYS_LIVE_MODEL", "").strip()
_API_KEY = os.environ.get("MNERYS_LIVE_API_KEY", "").strip()

_MISSING = [name for name, value in (
    ("MNERYS_LIVE_BASE_URL", _BASE_URL), ("MNERYS_LIVE_MESSAGES_URL", _MESSAGES_URL),
    ("MNERYS_LIVE_MODEL", _MODEL), ("MNERYS_LIVE_API_KEY", _API_KEY),
) if not value]
if _MISSING:
    print("LIVE-BLOCKED: missing environment configuration: " + ", ".join(_MISSING))
    sys.exit(2)

# Disposable application data + single-user authority, BEFORE src imports.
_WORKSPACE_PARENT = tempfile.mkdtemp(prefix="mnerys-live-")
os.environ["ODYSSEUS_DATA_DIR"] = tempfile.mkdtemp(prefix="mnerys-live-data-")
os.environ["AUTH_ENABLED"] = "false"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from src.agent_loop import stream_agent_loop  # noqa: E402
from src.tool_registry import project_tools_for_wire  # noqa: E402
from src.tool_policy import ToolPolicy  # noqa: E402


def _sse_frames(chunks):
    for chunk in chunks:
        line = chunk.strip()
        if not line.startswith("data: "):
            continue
        body = line[len("data: "):]
        if body == "[DONE]":
            yield ("done", None)
            continue
        try:
            yield ("frame", json.loads(body))
        except ValueError:
            continue


def _collect(endpoint_url, prompt, *, workspace, rounds=8):
    """Run one real streaming turn; return redacted execution evidence."""
    evidence = {
        "tool_starts": [], "tool_outputs": [], "deltas": [],
        "rounds": 0, "done": False, "errors": [], "final_response": "",
    }
    messages = [{"role": "user", "content": prompt}]

    async def run():
        loop_chunks = stream_agent_loop(
            endpoint_url, _MODEL, messages,
            headers={"Authorization": f"Bearer {_API_KEY}"},
            temperature=0.2, max_tokens=1024,
            owner="live-acceptance", session_id=None,
            workspace=workspace, tool_policy=ToolPolicy(),
            max_rounds=rounds,
        )
        async for chunk in loop_chunks:
            for kind, frame in _sse_frames([chunk] if isinstance(chunk, str) else []):
                if kind == "done":
                    evidence["done"] = True
                elif kind == "frame":
                    ftype = frame.get("type")
                    if ftype == "tool_start":
                        evidence["tool_starts"].append({
                            "tool": frame.get("tool"), "round": frame.get("round"),
                        })
                    elif ftype == "tool_output":
                        output = str(frame.get("output") or "")
                        evidence["tool_outputs"].append({
                            "tool": frame.get("tool"),
                            "output_prefix": output[:160],
                            "exit_hint": "error" in output.lower()[:80],
                        })
                    elif ftype == "agent_step":
                        evidence["rounds"] += 1
                    elif ftype == "tool_approval" or frame.get("approval_id"):
                        evidence["errors"].append(
                            f"approval-gate: {str(frame.get('question') or '')[:60]} "
                            f"tool={frame.get('tool')}"
                        )
                    elif ftype == "metrics":
                        data = frame.get("data") or {}
                        evidence["metrics"] = {
                            k: data.get(k) for k in
                            ("model", "agent_rounds", "tool_calls", "total_time")
                        }
                    elif ftype == "final_response":
                        evidence["final_response"] = str(frame.get("content") or "")
                    elif ftype == "delta":
                        evidence["deltas"].append(str(frame.get("delta") or ""))
                    elif ftype in ("error", "event") or frame.get("error"):
                        evidence["errors"].append(str(frame)[:200])
                # frames from CoreNativeEvent objects are handled by their str form

    asyncio.run(run())
    if not evidence["final_response"]:
        evidence["final_response"] = "".join(evidence["deltas"])[-400:]
    return evidence


def _advertised_canonical_names():
    schemas = project_tools_for_wire([
        {"type": "function", "function": {"name": h, "parameters": {}}}
        for h in ("read_file", "write_file", "edit_file", "glob", "grep", "bash",
                  "web_fetch", "web_search", "tool_search", "activate_skill")
    ])
    return [s["function"]["name"] for s in schemas]


def _family_report(name, url, workspace):
    token = f"MNERY-LIVE-{uuid.uuid4().hex[:8].upper()}"
    token2 = f"MNERY-EDIT-{uuid.uuid4().hex[:8].upper()}"
    report = {"family": name, "endpoint_declared": url, "model": _MODEL}

    # 1. Write -> Read -> Edit sequence with real execution + continuation.
    t0 = time.time()
    write_read = None
    for attempt in range(2):
        write_read = _collect(
            url,
            f"Create the file {workspace}/live-report.md using ONLY the Write tool "
            f"(fenced ```Write with the path on the first line, content after; "
            f"content is exactly {token}, no markdown fences inside). Never use bash, "
            f"sed, or heredocs for this. After the Write succeeds, read the file with "
            f"ONLY the Read tool. Then change {token} to {token2} using ONLY the Edit "
            f"tool with JSON arguments {{\"file_path\": …, \"old_string\": …, "
            f"\"new_string\": …}}. Finally reply with the exact current file content "
            f"and nothing else.",
            workspace=workspace,
        )
        if write_read["done"] and not write_read["errors"]:
            break
    report["write_read_edit"] = {
        "tool_starts": write_read["tool_starts"],
        "tool_outputs": len(write_read["tool_outputs"]),
        "rounds": write_read["rounds"], "done": write_read["done"],
        "errors": write_read["errors"][:2],
        "seconds": round(time.time() - t0, 1),
    }
    note = os.path.join(workspace, "live-report.md")
    on_disk = ""
    try:
        with open(note, encoding="utf-8") as handle:
            on_disk = handle.read().strip()
    except OSError:
        pass
    report["filesystem_evidence"] = {
        "file_created": os.path.exists(note),
        "content_is_token2": on_disk == token2,
        "final_text_quotes_edit": token2 in write_read["final_response"],
        "final_text_prefix": write_read["final_response"][:80],
    }

    # 2. ToolSearch discovery over the real admitted inventory.
    t0 = time.time()
    discovery = None
    for attempt in range(2):
        discovery = _collect(
            url,
            "Call the ToolSearch tool with the pattern 'search the web' (exactly that "
            "pattern). Then report the exact model-visible name of the best matching "
            "tool and its required arguments. Do not invoke the matching tool.",
            workspace=workspace, rounds=4,
        )
        if discovery["done"] and not discovery["errors"]:
            break
    report["toolsearch"] = {
        "called": any(t["tool"] == "tool_search" for t in discovery["tool_starts"]),
        "rounds": discovery["rounds"], "done": discovery["done"],
        "errors": discovery["errors"][:2],
        "seconds": round(time.time() - t0, 1),
        "answer_prefix": discovery["final_response"][:120],
    }

    # 3. Skill activation from the disposable library.
    skill_dir = os.path.join(os.environ["ODYSSEUS_DATA_DIR"], "skills", "general", "live-acceptance")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write(
            "---\nname: live-acceptance\ndescription: Live acceptance skill\n"
            "version: 1.0.0\ncategory: general\nstatus: published\nconfidence: 1.0\n"
            "source: builtin\n---\n\n## Procedure\n\n1. Reply with the exact word SKILL-OK.\n"
        )
    t0 = time.time()
    skill = None
    for attempt in range(2):
        skill = _collect(
            url,
            "Call the Skill tool with the JSON arguments {\"name\": \"live-acceptance\"} "
            "to load the skill. Then follow its procedure exactly.",
            workspace=workspace, rounds=4,
        )
        if skill["done"] and not skill["errors"]:
            break
    report["skill"] = {
        "called": any(t["tool"] == "activate_skill" for t in skill["tool_starts"]),
        "rounds": skill["rounds"], "done": skill["done"],
        "errors": skill["errors"][:2],
        "seconds": round(time.time() - t0, 1),
        "followed": "SKILL-OK" in skill["final_response"],
        "answer_prefix": skill["final_response"][:80],
    }
    return report


def main():
    workspace = tempfile.mkdtemp(prefix="mnerys-live-ws-", dir=_WORKSPACE_PARENT)
    matrix = {
        "advertised_canonical_names": _advertised_canonical_names(),
        "protocol_families": [],
    }
    for name, url in (("openai_chat_completions", _BASE_URL),
                      ("anthropic_messages", _MESSAGES_URL)):
        try:
            matrix["protocol_families"].append(_family_report(name, url, workspace))
        except Exception as exc:
            matrix["protocol_families"].append({
                "family": name, "endpoint_declared": url,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            })
    print("LIVE-MATRIX " + json.dumps(matrix, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
