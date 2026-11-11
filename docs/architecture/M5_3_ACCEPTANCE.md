# M5.3 — Formal Acceptance Report (Increment 6)

Formal M5.3 exit-gate validation. Evidence classes: **LIVE** (real provider
calls through the real loop, M5.2 increment 6/7 harness), **E2E** (the REAL
agent loop + risk classifier + approval store + real Bash executor with only
the model scripted — `tests/test_m53_gateway_acceptance.py`), **DET**
(deterministic focused tests), **SRC** (audited source contract).

## Exit-gate matrix

| # | Requirement | Result | Evidence |
| - | --- | --- | --- |
| 1 | Natural HOME/CWD defaults | **PASS** | `src/session_cwd.py` (DET: per-surface defaults — CLI/TUI `pwd`, Web/Telegram/remote HOME, unusable-`pwd` fallback, root/sensitive/nonexistent refusals); route integration (DET: create/PATCH/v1 tests) |
| 2 | Persistent session directory across clients | **PASS** | `sessions.cwd` persisted; first-bind persistence (DET); resume/restart (DET: row + detail assertions); v1 round-trip (DET) |
| 3 | Local and remote transport behavior | **PASS** | remote never inherits the client `pwd` (DET); the shared v1 contract serves CLI/TUI (DET + the gateway-sessions suite); the TUI `session_cwd` bridge contract (SRC + suite) |
| 4 | Actual risky-command approval prompts | **PASS** | E2E: `rm -rf build` through the REAL loop → genuine pending with the risk reason + workspace; the operator's Allow-Once resumed the SEALED operation which executed **exactly once** against the real filesystem (the disposable build dir removed) and the replay attempt refused |
| 5 | All four approval decisions functioning | **PASS** | E2E: allow-once (exactly-once + replay refusal), deny (no execution, not revivable); DET: session/always/deny scope mapping, persistence, revocation, restart survival, dedup, per-owner bounds, fail-closed malformed stores |
| 6 | Deterministic local risk classification | **PASS** | `src/command_risk.py` — 68 DET (14 low-risk recognitions, ~40 detections with exact levels, false-positive checks, conservative fallbacks, pipelines/substitutions/nested shells, python AST) + the classifier wired into `ToolRunSecurityContext.decision_for` for every execution surface |
| 7 | Explicit opt-in YOLO | **PASS** | E2E: YOLO-ON executed the risky command with NO prompt and the audited suppression; DET: default OFF, confirmed enable, revision fencing (409), restart persistence, session isolation, no inheritance |
| 8 | YOLO/deny/owner-boundary negatives | **PASS** | DET+E2E: DENIED risk classifications refuse with YOLO ON; delegated boundaries refuse; policy denials survive; unknown tools under external context refuse; cross-owner toggles 403; read-before-edit enforced downstream of YOLO |
| 9 | Unified Agent-first experience | **PASS** | `resolve_unified_chat_mode` normalization (DET: legacy chat/empty/unknown → agent); the composer toggle removed (SRC + Node tests); the tool-error auto-switch removed; tool toggles always visible; `.mode-toggle` CSS preserved for other panels |
| 10 | Legacy Chat compatibility | **PASS** | `mode=chat` accepted (normalization, never an engine selector); the demoted caller's text-only ceiling audited (`tools=None` adapter); the strict `gateway_profile=text_v1` cutover keeps its RAW legacy-mode contract (71 cutover tests); historical transcripts/mode pills readable |

## Real execution evidence (the binding scenarios)

1. **Harmless command, no prompt**: `echo hello-risk-acceptance` through the
   real loop + dispatcher → `exit_code: 0`, output threaded, no approval card.
2. **Risky command → genuine approval → exactly-once execution**:
   `rm -rf build` → the risk gate refused with "Dangerous operation requires
   approval: Recursive deletion."; the pending bound owner/session/tool/
   content/workspace; the operator's Allow-Once resumed through the sealed
   authority (the ORIGINAL request identity — a resume under a different
   request identity is refused: "Approved backend binding changed"); the real
   `rm -rf` executed once in the disposable workspace (dir gone); the model's
   replay attempt was refused by the one-use claim.
3. **Deny**: no execution, the disposable file survived, the prompt cannot be
   revived into a grant.
4. **YOLO ON**: the same risky command executed with no prompt; the audit
   recorded "YOLO suppressed risk approval" with the run/tool/level/reason
   (no command content in the audit line).
5. **Sensitive path with YOLO ON**: `/etc/shadow` never read.

## Client coverage

- **Web**: real (the composer/indicator/cards are in-repo and Node-tested).
- **CLI/TUI**: the shared Gateway v1 contract is verified server-side (the
  gateway-sessions and TUI-session suites); the TUI/CLI clients are external
  packages — their rendering is covered by their own repos. Server-side
  parity for session creation, CWD, approvals, YOLO and tool events is
  verified through the shared API. **Limitation**: no in-repo CLI/TUI visual
  verification.
- **Desktop/mobile**: the affected web surfaces are static no-build HTML/CSS
  with the existing responsive layout untouched (only the toggle's removal);
  Node architecture tests verify the composer structure. **Limitation**: no
  browser screenshot verification in this environment.

## Security findings and defects corrected during M5.3

1. Session `cwd` was stored unvalidated → validated server-side (increment 1).
2. Exact-approval resume required an ARMED context unconditionally →
   context-EQUALITY invariant (a risk-gated approval sealed in a clean run
   was unresumable; found by the formal acceptance e2e — increment 6).
3. Tool-selection fallbacks dropped caller-routed tools on a cold/slow
   embedding index (a real flake) → union fixes + regression (increment 4).
4. Canonical-name prescriptions derived no grants → the allow-side
   normalization (increment 6 of M5.2, hardened in M5.3).

## Focused test evidence (increment 6 final round)

652 + 6 new e2e tests — **658 passed / 0 failed** across: session CWD (25),
approval decisions/rules (24), command risk (69), YOLO (13), Agent-first
(12), external-context gate, exact approvals, interactive gates, gateway
admission/approval authority, gateway sessions v1, session persistence, the
text cutover suites, final tool policy, chat-route policy, read-before-edit,
derived artifacts, resource bindings, discovery/skills.

## Formal decision

**M5.3: PASS.** All ten binding exit-gate requirements validated with real
Gateway execution evidence and focused deterministic coverage. Known
limitations (documented, non-binding): external CLI/TUI visual verification,
browser screenshot verification, and the classifier's opaque-blob boundary
(enforced by the sandbox, not classification).

## Post-release user acceptance and corrective gate (2026-11-11)

The deployed v0.19.0 revealed four defects absent from the original M5.3
English-only acceptance:

1. Colloquial PT-BR requests to list a directory or confirm a local task
   lost their read grants even though the model was offered tools.
2. The Web prefetch searched public websites for local shell terms and
   acknowledgments because the enabled search toggle was treated as intent.
3. The durable Web projection accepted only the legacy approve, approve_task
   and deny decisions; valid allow_once/always_allow choices failed after
   their in-memory seal was consumed. A projected-settlement early return
   additionally skipped persistence of the exact reviewed rule.
4. Failed filesystem tools did not prevent the model from asserting
   directory contents without evidence.

The correction preserves the M5.3 security boundaries: listing requests admit
only directory-read tools, not general Bash or write capabilities; copy
requests remain bound to user intent and existing risk/approval/resource gates.
Short acknowledgments inherit only a recent trusted user task, never model
or external tool prose. Web and durable approval paths use one decision catalog,
and projected always-allow settlement no longer skips exact-rule persistence.
Persisted tool events now distinguish blocked from executed operations; failed
local inspections cannot be reported as verified directory contents.

**Focused evidence:** 130 authorization/approval tests, 259 Web/Agent-first
tests, 28 PostgreSQL 17 isolated turn-record tests; compilation and diff checks
green. The original v0.19.0 milestone PASS is historical evidence, not proof
of this corrective deployment. Hosted release and authenticated production
re-verification remain distinct acceptance gates.
