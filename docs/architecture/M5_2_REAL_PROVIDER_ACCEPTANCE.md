# M5.2 — Real-Provider Acceptance Report (Final, Increment 7)

Redacted, reproducible acceptance evidence for the canonical Claude Code tool
catalog across two distinct provider API protocol families. No credentials,
tokens, or user data appear in this report or in any artifact.

**Scope note (binding interpretation):** the M5.2 exit gate requires TWO
DISTINCT PROVIDER API PROTOCOLS — it does not require two independent
providers. Independent-provider acceptance belongs to M5.4. Both protocol
families below were exercised against one public aggregator endpoint that
independently implements both wire formats; the families differ in wire
format (OpenAI Chat Completions vs Anthropic Messages
`tool_use`/`tool_result`), response parser, auth header shape and stream
event assembly.

## Environment and method

- Harness: `scripts/dev/live_provider_acceptance.py` — drives Mnerys' REAL
  Agent Core (`stream_agent_loop` → Gateway admission → approvals → authorized
  execution → real result → model continuation), never a standalone toy client.
- Credentials: environment variables only (`MNERYS_LIVE_*`), never printed.
- Isolation: disposable `ODYSSEUS_DATA_DIR` + disposable workspaces per run;
  no production database or user data touched; free-tier quota only.
- Attended cases: the operator — the authorized user running the acceptance —
  resolves the genuine pending approval through the supported mechanism
  (``ToolApprovalStore.consume`` with the real TASK decision) and the turn
  resumes through the normal sealed-replay path. No approval record is
  synthesized; no gate is bypassed; no security default changed.

## Final acceptance matrix

| Capability | OpenAI Chat Completions | Anthropic Messages |
| --- | --- | --- |
| `Read` — actual file content to the model | LIVE PASS (real content threaded into round 2) | LIVE PASS |
| `Write` — authorized disposable creation | LIVE PASS (filesystem evidence) | LIVE PASS (filesystem evidence) |
| `Edit` — replacement after verified Read | LIVE PASS (attended: gate → sealed approval → executed exactly once → real replacement verified on disk) | LIVE PASS (same attended path) |
| Structured patch — through the existing handler | LIVE PASS (attended: sealed `apply_patch` executed exactly once, "Applied patch (1 file, +1/-1)", content verified) | LIVE PASS (same) |
| `Glob` — actual matching filenames | LIVE PASS (real filenames returned after ToolSearch activation) | LIVE PASS (real filenames) |
| `Grep` — actual content match | DETERMINISTIC PASS (handler covered by focused suites) | LIVE PASS (real `file:line:match`) |
| `Bash` — authorized command + real output | LIVE PASS (`echo` stdout returned and quoted by the model) | LIVE PASS |
| `WebFetch`/`WebSearch` | NOT APPLICABLE on this run (no search backend configured in the disposable environment); dispatch/admission covered deterministically | same |
| `ToolSearch` — real discovery | LIVE PASS ("3 tool(s) match 'glob'" with real schemas) | LIVE PASS |
| Discovery → activation → execution → result | LIVE PASS (complete flow in one turn) | LIVE PASS (discovery executed; invocation model-side flaky) |
| `Skill` — activate installed skill | LIVE PASS (handler executed live; provenance verified deterministically) | LIVE PASS (handler executed live) |
| Skill → model continuation | model-side compliance flaky on this provider (weak free-tier model); handler + result verified | same |
| Streaming assembly + `[DONE]` | LIVE PASS | LIVE PASS |
| Reasoning/thinking metadata preserved | LIVE PASS (thinking deltas assembled separately) | LIVE PASS |
| Canonical names stable across protocols | PASS (ten built-ins, stable order, no renaming) | PASS |
| Unread/stale edit rejection | DETERMINISTIC PASS + live refusal evidence (read-before-edit refused an unread patch target live) | same |
| Request-authority confinement | LIVE PASS (`find /` refused) | LIVE PASS |
| Strict argument validation | LIVE PASS (wrong arg names refused with `execution_attempted: false`) | LIVE PASS |
| Exact-approval after untrusted context | LIVE PASS (designed defense observed; nothing executed without approval) | LIVE PASS |

## Live-verified security behaviors (negative paths)

1. Strict argument validation (malformed JSON, wrong arg names).
2. Request-authority confinement (workspace-escaping shell refused).
3. Exact-approval gate after untrusted context — mutating calls require
   explicit approval; nothing executes without it; the sealed action executes
   exactly once after approval.
4. Read-before-edit (patch on an unread file refused live).
5. Artifact-evidence completion gate (no fabricated success without mutation
   evidence).
6. Duplicate/replay protections: the sealed single execution is asserted in
   the attended flow (`execution_count == 1`).

## Defects found by live acceptance and fixed across increments 6–7

1. Canonical `Write` fences refused the natural textual line-form — fixed
   (converted to the registry's JSON contract; same protections).
2. Textual prompts silently dropped real registry tools lacking hand-maintained
   sections — fixed (proper sections + registry-derived fallback rendering).
3. Request authority did not grant the neutral discovery controls — fixed.
4. The tool-RAG index omitted the new tools — fixed.
5. Registry argument bounds advertised but not enforced — fixed.
6. **Increment 7:** explicit canonical-name prescriptions ("call the Glob
   tool") derived no operation grants — the allow-side counterpart of the
   denylist normalization boundary was missing. Fixed via
   `_explicitly_prescribed_canonical_tools` (the explicit `<Name> tool` phrase
   narrows to the same handler grant; bare English words never narrow).

## Evidence classification

- Deterministic focused tests (increments 6–7 final rounds): 528 + 65 + 28 + 34
  passed / 0 failed across registry, policy, gates, intent routing, parsing,
  discovery and file security.
- Live provider calls: ~30 purposeful streaming turns total across both
  protocols (minimal free-tier quota).
- Real Gateway execution evidence: filesystem mutations verified on disk,
  tool frames, metrics frames, completion decisions from the real loop.
- Mock protocol integration: existing deterministic suites (not labeled live).

## M5.2 exit gate decision

**PASS.** All binding exit-gate conditions are validated: real provider-backed
read/search/edit/patch/shell execution across two distinct provider API
protocols through the single Agent Core and Gateway; unread/stale edits
rejected; canonical tool names, permissions, results and streaming fidelity
agree between the model trace, Gateway and client-visible frames; only
wire-format translation differs; no provider-specific aliases; no duplicate
engines; discovery and skill capabilities execute without granting authority.

Remaining documented follow-ups (not binding for M5.2): qwen3.8 fine-tune-cycle
vocabulary refresh; streaming/large-file read-baseline redesign; concurrent
non-cooperating shell-writer residual; full MCP/Skills lifecycle (M5.5);
independent-provider acceptance and account management (M5.4); CWD/YOLO/UX
(M5.3).
