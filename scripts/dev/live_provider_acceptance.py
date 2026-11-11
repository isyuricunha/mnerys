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
from src.tool_approvals import tool_approval_store  # noqa: E402
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


def _collect(endpoint_url, prompt, *, workspace, rounds=8, messages=None,
             exact_approval=None, session_id=None, store=None, proposals=None):
    """Run one real streaming turn; return redacted execution evidence."""
    evidence = {
        "tool_starts": [], "tool_outputs": [], "deltas": [],
        "rounds": 0, "done": False, "errors": [], "final_response": "",
        "approval_id": "", "round_deltas": {},
    }
    if messages is None:
        messages = [{"role": "user", "content": prompt}]

    async def run():
        loop_chunks = stream_agent_loop(
            endpoint_url, _MODEL, messages,
            headers={"Authorization": f"Bearer {_API_KEY}"},
            temperature=0.2, max_tokens=1024,
            owner="live-acceptance", session_id=session_id,
            workspace=workspace, tool_policy=ToolPolicy(),
            max_rounds=rounds,
            exact_approval=exact_approval,
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
                    elif ftype == "metrics":
                        data = frame.get("data") or {}
                        evidence["metrics"] = {
                            k: data.get(k) for k in
                            ("model", "agent_rounds", "tool_calls", "total_time")
                        }
                    elif ftype == "final_response":
                        evidence["final_response"] = str(frame.get("content") or "")
                    elif ftype == "delta":
                        round_num = frame.get("round")
                        bucket = evidence["round_deltas"].setdefault(round_num, {"text": "", "thinking": ""})
                        if frame.get("thinking"):
                            bucket["thinking"] += str(frame.get("delta") or "")
                        else:
                            bucket["text"] += str(frame.get("delta") or "")
                        evidence["deltas"].append(str(frame.get("delta") or ""))
                    elif ftype == "error" or frame.get("error"):
                        evidence["errors"].append(str(frame)[:200])
                # frames from CoreNativeEvent objects are handled by their str form

    observer = (
        lambda pending, superseded: proposals.append(pending)
    ) if proposals is not None else None
    if observer is not None:
        from src.tool_approvals import observe_approval_proposals

        with observe_approval_proposals(observer):
            asyncio.run(run())
    else:
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

    # 4. Search tools: Glob then Grep, real matching evidence.
    t0 = time.time()
    with open(os.path.join(workspace, "search-target.md"), "w", encoding="utf-8") as handle:
        handle.write(f"needle-{uuid.uuid4().hex[:6]} unique haystack\n")
    search = _collect(
        url,
        f"Use ONLY the Glob tool with JSON arguments {{\"pattern\": \"**/*.md\"}}. "
        f"After it returns, use ONLY the Grep tool with JSON arguments "
        f"{{\"pattern\": \"needle-\"}}. Report the matching filenames from each. "
        f"Never use bash.",
        workspace=workspace, rounds=5,
    )
    report["search_tools"] = {
        "glob_called": any(t["tool"] == "glob" for t in search["tool_starts"]),
        "grep_called": any(t["tool"] == "grep" for t in search["tool_starts"]),
        "outputs": [
            {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
            for t in search["tool_outputs"]
        ],
        "done": search["done"], "errors": search["errors"][:2],
        "seconds": round(time.time() - t0, 1),
    }

    # 5. Shell: one authorized harmless command with real stdout evidence.
    t0 = time.time()
    marker = f"MNERY-SH-{uuid.uuid4().hex[:6].upper()}"
    shell = _collect(
        url,
        f"Use ONLY the Bash tool to run exactly: echo {marker}. Reply with the exact "
        f"command output.",
        workspace=workspace, rounds=3,
    )
    report["shell"] = {
        "called": any(t["tool"] == "bash" for t in shell["tool_starts"]),
        "outputs": [
            {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
            for t in shell["tool_outputs"]
        ],
        "stdout_returned": marker in shell["final_response"] or any(
            marker in t["output_prefix"] for t in shell["tool_outputs"]
        ),
        "done": shell["done"], "errors": shell["errors"][:2],
        "seconds": round(time.time() - t0, 1),
    }

    # 6. Discovery -> activation -> invocation -> real result, one flow.
    t0 = time.time()
    discovery_flow = _collect(
        url,
        f"First call the ToolSearch tool with JSON arguments {{\"pattern\": \"glob\"}}. "
        f"After the results arrive, call the Glob tool with JSON arguments "
        f"{{\"pattern\": \"**/*.md\"}}. Report the filenames the Glob tool returned.",
        workspace=workspace, rounds=5,
    )
    report["discovery_invoke"] = {
        "toolsearch_called": any(
            t["tool"] == "tool_search" for t in discovery_flow["tool_starts"]),
        "glob_called": any(t["tool"] == "glob" for t in discovery_flow["tool_starts"]),
        "outputs": [
            {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
            for t in discovery_flow["tool_outputs"]
        ],
        "done": discovery_flow["done"], "errors": discovery_flow["errors"][:2],
        "seconds": round(time.time() - t0, 1),
    }

    # 7. Structured patch: real multi-hunk patch through the patch handler.
    t0 = time.time()
    patch_token = f"PATCHED-{uuid.uuid4().hex[:6].upper()}"
    patch_target = os.path.join(workspace, "patch-me.py")
    with open(patch_target, "w", encoding="utf-8") as handle:
        handle.write("def greet():\n    return 'ORIGINAL-VALUE'\n")
    patch = _collect(
        url,
        f"Use ONLY the apply_patch tool to change the string ORIGINAL-VALUE to "
        f"{patch_token} in {workspace}/patch-me.py. Use the exact fenced apply_patch "
        f"format with an Update File section. Then reply with the function's new "
        f"return value. Never use bash, sed, or the Edit tool for this.",
        workspace=workspace, rounds=4,
    )
    on_disk = ""
    try:
        with open(patch_target, encoding="utf-8") as handle:
            on_disk = handle.read()
    except OSError:
        pass
    report["structured_patch"] = {
        "called": any(t["tool"] == "apply_patch" for t in patch["tool_starts"]),
        "applied": patch_token in on_disk,
        "outputs": [
            {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
            for t in patch["tool_outputs"]
        ],
        "done": patch["done"], "errors": patch["errors"][:2],
        "seconds": round(time.time() - t0, 1),
    }
    return report


def _attended_edit(url, workspace, session_id):
    """Attended Edit acceptance through the REAL approval workflow.

    The file is pre-created deterministically; turn 1 is Read (its workspace
    result legitimately arms the untrusted-content gate) then Edit — the Edit
    stops at the exact-approval prompt with a sealed pending operation. The
    operator — the authorized user running this acceptance — approves through
    the supported mechanism (``ToolApprovalStore.consume`` with the genuine
    TASK decision). Turn 2 resumes through the normal sealed-replay path: the
    sealed (tool, content) pair executes exactly once and the real result
    threads into continuation. No approval record is synthesized and no gate
    is bypassed.
    """
    from src.tool_approvals import TASK_APPROVAL_DECISION

    token = f"MNERY-ATT-{uuid.uuid4().hex[:8].upper()}"
    token2 = f"MNERY-ATT-{uuid.uuid4().hex[:8].upper()}"
    note = os.path.join(workspace, "attended.md")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write(token)
    prompt = (
        f"First use ONLY the Read tool to read {workspace}/attended.md. After you "
        f"see its content, use ONLY the Edit tool (JSON arguments: file_path, "
        f"old_string, new_string) to change {token} to {token2}. Then reply with "
        f"the exact current file content. Never use bash, sed or heredocs, and "
        f"never use Write."
    )
    proposals = []
    turn1 = _collect(
        url, prompt, workspace=workspace, rounds=6,
        session_id=session_id, proposals=proposals,
    )
    gated_tool = None
    for proposal in proposals:
        gated_tool = proposal.tool_name
        break
    approval_id = proposals[0].approval_id if proposals else ""
    pending = tool_approval_store.peek(approval_id) if approval_id else None
    gate_evidence = {
        "gated_tool": gated_tool,
        "sealed_tool": pending.tool_name if pending else None,
        "sealed_workspace": (pending.workspace if pending else None) == workspace,
        "approval_id_present": bool(approval_id),
        "turn1_tool_starts": turn1["tool_starts"],
        "turn1_tool_outputs": [
            {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
            for t in turn1["tool_outputs"]
        ],
        "turn1_final": turn1["final_response"][:80],
    }
    if not approval_id or pending is None:
        return {
            "gate": gate_evidence,
            "resume": {"skipped": "no pending approval was created"},
            "turn1_tool_starts": turn1["tool_starts"],
            "turn1_rounds": turn1["rounds"],
            "turn1_final": turn1["final_response"][:100],
            "turn1_errors": turn1["errors"][:2],
        }

    # The authorized user approves the genuine pending operation.
    approval = tool_approval_store.consume(
        approval_id,
        decision=TASK_APPROVAL_DECISION,
        owner="live-acceptance",
        session_id=session_id,
        allow_continuation=True,
    )
    if approval is None:
        return {"gate": gate_evidence, "resume": {"skipped": "approval could not be resolved"}}

    last_round = max((r for r in turn1["round_deltas"] if r is not None), default=1)
    assistant_text = turn1["round_deltas"].get(last_round, {}).get("text", "")
    resume_messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": assistant_text},
        {"role": "user", "content": "Approved. Continue with exactly the approved action."},
    ]
    turn2 = _collect(
        url, None, workspace=workspace, rounds=6, session_id=session_id,
        messages=resume_messages, exact_approval=approval,
    )
    on_disk = ""
    try:
        with open(note, encoding="utf-8") as handle:
            on_disk = handle.read().strip()
    except OSError:
        pass
    edit_starts = [t for t in turn2["tool_starts"] if t["tool"] == "edit_file"]
    return {
        "gate": {
            **gate_evidence,
            "turn1_tool_starts": turn1["tool_starts"],
            "turn1_final": turn1["final_response"][:80],
        },
        "resume": {
            "edit_executed": bool(edit_starts),
            "edit_execution_count": len(edit_starts),
            "content_is_token2": on_disk == token2,
            "tool_outputs": [
                {"tool": t["tool"], "prefix": t["output_prefix"][:80]}
                for t in turn2["tool_outputs"]
            ],
            "rounds": turn2["rounds"], "done": turn2["done"],
            "errors": turn2["errors"][:2],
            "final_prefix": turn2["final_response"][:100],
        },
    }


def _attended_patch(url, workspace, session_id):
    """Attended structured-patch acceptance through the REAL approval workflow.

    Same attended shape as `_attended_edit`, but the sealed operation is a
    multi-line apply_patch through the existing patch handler and its security
    boundary (read-before-edit baseline + workspace confinement + diff).
    """
    from src.tool_approvals import TASK_APPROVAL_DECISION

    token2 = f"PATCHED-{uuid.uuid4().hex[:6].upper()}"
    patch_target = os.path.join(workspace, "patch-me.py")
    with open(patch_target, "w", encoding="utf-8") as handle:
        handle.write("def greet():\n    return 'ORIGINAL-VALUE'\n")
    prompt = (
        f"First use ONLY the Read tool to read {workspace}/patch-me.py. After you see "
        f"its content, use ONLY the apply_patch tool with this exact patch:\n"
        f"*** Begin Patch\n*** Update File: {workspace}/patch-me.py\n@@\n-    return "
        f"'ORIGINAL-VALUE'\n+    return '{token2}'\n*** End Patch\n"
        f"Then reply with the function's new return value. Never use bash, sed, or the "
        f"Edit tool."
    )
    proposals = []
    turn1 = _collect(
        url, prompt, workspace=workspace, rounds=6,
        session_id=session_id, proposals=proposals,
    )
    approval_id = proposals[0].approval_id if proposals else ""
    pending = tool_approval_store.peek(approval_id) if approval_id else None
    gate_evidence = {
        "gated_tool": proposals[0].tool_name if proposals else None,
        "sealed_tool": pending.tool_name if pending else None,
        "sealed_workspace": (pending.workspace if pending else None) == workspace,
        "approval_id_present": bool(approval_id),
        "turn1_tool_starts": turn1["tool_starts"],
        "turn1_final": turn1["final_response"][:80],
    }
    if not approval_id or pending is None:
        return {
            "gate": gate_evidence,
            "resume": {"skipped": "no pending approval was created"},
            "turn1_tool_outputs": [
                {"tool": t["tool"], "prefix": t["output_prefix"][:70]}
                for t in turn1["tool_outputs"]
            ],
            "turn1_rounds": turn1["rounds"],
        }
    approval = tool_approval_store.consume(
        approval_id,
        decision=TASK_APPROVAL_DECISION,
        owner="live-acceptance",
        session_id=session_id,
        allow_continuation=True,
    )
    if approval is None:
        return {"gate": gate_evidence, "resume": {"skipped": "approval could not be resolved"}}
    last_round = max((r for r in turn1["round_deltas"] if r is not None), default=1)
    assistant_text = turn1["round_deltas"].get(last_round, {}).get("text", "")
    resume_messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": assistant_text},
        {"role": "user", "content": "Approved. Continue with exactly the approved action."},
    ]
    turn2 = _collect(
        url, None, workspace=workspace, rounds=6, session_id=session_id,
        messages=resume_messages, exact_approval=approval,
    )
    on_disk = ""
    try:
        with open(patch_target, encoding="utf-8") as handle:
            on_disk = handle.read()
    except OSError:
        pass
    patch_starts = [t for t in turn2["tool_starts"] if t["tool"] == "apply_patch"]
    return {
        "gate": gate_evidence,
        "resume": {
            "patch_executed": bool(patch_starts),
            "patch_execution_count": len(patch_starts),
            "applied": token2 in on_disk,
            "tool_outputs": [
                {"tool": t["tool"], "prefix": t["output_prefix"][:80]}
                for t in turn2["tool_outputs"]
            ],
            "rounds": turn2["rounds"], "done": turn2["done"],
            "errors": turn2["errors"][:2],
            "final_prefix": turn2["final_response"][:100],
        },
    }


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
    # Attended Edit acceptance (exact-approval workflow) per protocol family.
    matrix["attended_edit"] = []
    matrix["attended_patch"] = []
    for name, url in (("openai_chat_completions", _BASE_URL),
                      ("anthropic_messages", _MESSAGES_URL)):
        ws = tempfile.mkdtemp(prefix="mnerys-live-att-", dir=_WORKSPACE_PARENT)
        try:
            matrix["attended_edit"].append(
                {"family": name, **_attended_edit(url, ws, session_id=f"live-{uuid.uuid4().hex[:8]}")}
            )
        except Exception as exc:
            matrix["attended_edit"].append({
                "family": name, "error": f"{type(exc).__name__}: {exc}"[:200],
            })
    for name, url in (("openai_chat_completions", _BASE_URL),
                      ("anthropic_messages", _MESSAGES_URL)):
        ws = tempfile.mkdtemp(prefix="mnerys-live-patch-", dir=_WORKSPACE_PARENT)
        try:
            matrix["attended_patch"].append(
                {"family": name, **_attended_patch(url, ws, session_id=f"live-{uuid.uuid4().hex[:8]}")}
            )
        except Exception as exc:
            matrix["attended_patch"].append({
                "family": name, "error": f"{type(exc).__name__}: {exc}"[:200],
            })
    print("LIVE-MATRIX " + json.dumps(matrix, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
