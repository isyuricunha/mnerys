# M5.2 — Real-Provider Acceptance Report (Increment 6)

Redacted, reproducible acceptance evidence for the canonical Claude Code tool
catalog across distinct provider API protocol families. No credentials,
hostnames beyond the public endpoint's purpose, tokens, or user data appear in
this report or in any artifact.

## Environment and method

- Harness: `scripts/dev/live_provider_acceptance.py` — drives Mnerys' REAL
  Agent Core (`stream_agent_loop` → Gateway admission → authorized execution
  → real result → model continuation), not a standalone toy client.
- Credentials: read from the environment at run time only (`MNERYS_LIVE_*`),
  never printed, logged or committed.
- Isolation: disposable `ODYSSEUS_DATA_DIR` and disposable workspace
  directories per run; no production database or user data touched.
- The two protocol families were exercised against ONE public aggregator
  endpoint that independently implements both wire formats; the two families
  differ in wire format (OpenAI Chat Completions vs Anthropic Messages
  `tool_use`/`tool_result`), response parser, auth header shape and stream
  event assembly — they are distinct protocols, not distinct base URLs for
  the same wire format.

## Acceptance matrix

| Item | OpenAI Chat Completions | Anthropic Messages |
| --- | --- | --- |
| Endpoint declaration | `…/v1` | `…/v1/messages` (path-declared protocol) |
| Model (non-sensitive) | glm-5.3-flash | glm-5.3-flash |
| Canonical names advertised | ten built-ins (stable order) | ten built-ins (stable order) |
| `Write` executed live | YES (filesystem evidence: file created) | YES (filesystem evidence: file created) |
| `Read` executed live | YES (3 tool outputs threaded) | YES |
| `Edit` live | reached the exact-approval gate (see below) | reached the exact-approval gate (see below) |
| `Glob`/`Grep`/`Bash`/patch | deterministic dispatch coverage; `Bash` fence with JSON args refused live ("Bash has no argument 'name'") | same |
| `ToolSearch` called + executed | YES (real inventory results) | YES (real inventory results) |
| `Skill` | called live (activate_skill frame observed); model-side continuation flaky | model-side compliance flaky on this provider |
| Streaming | assembled natively, `[DONE]` reached | assembled natively, `[DONE]` reached |
| Model continuation after tool result | VERIFIED (round 2+ reasoning on real results) | VERIFIED |
| Policy/approval behavior | refused operations reported truthfully; nothing fabricated | same |
| Result | PASS | PASS |

## Live-verified security behaviors (negative paths)

1. **Strict argument validation** — a fenced ```Bash body carrying JSON
   `{"name": …}` was refused (`Bash has no argument 'name'`,
   `execution_attempted: false`). No fabricated success.
2. **Request authority** — a shell `find /` escaping the sealed workspace was
   refused ("outside server request authority"); nothing executed.
3. **Exact-approval gate after tool-result context** — after Write/Read tool
   results entered the conversation (untrusted content), the next MUTATING
   call (the Edit round) required exact user approval ("Allow this task to
   continue?"). Headless runs cannot approve; the turn reported the gate
   instead of executing. This is the designed prompt-injection defense,
   live-verified.
4. **Read-before-edit** — the model read the file before editing it
   (read_file frame preceded the edit round).
5. **Artifact-evidence completion gate** — when a prompt looked like a file
   mutation but no successful mutation evidence existed, the loop refused to
   report success ("required artifacts lack successful mutation evidence").

## Defects found by live acceptance and fixed

1. **Canonical `Write` fences refused the textual line-form** (path on the
   first line, content after) that models naturally emit — the same grammar
   the legacy alias always accepted. Fixed: the canonical spelling converts
   it to the registry's JSON contract, keeping read-before-edit and
   publication protections identical to native calls (trailing-whitespace
   caveat documented).
2. **Textual prompts silently dropped real tools** that had no hand-maintained
   `TOOL_SECTIONS` entry — the discovery controls were included in the turn
   surface but never rendered, so models could not see them. Fixed two ways:
   proper `TOOL_SECTIONS` entries with fenced examples for both tools, and a
   registry-derived fallback in `_assemble_prompt` so a future registry tool
   can never silently vanish from the prompt again.
3. **Request authority did not grant the discovery controls** — ToolSearch
   dispatch was refused ("outside server request authority"). Fixed: the
   controls are neutral grants (like `ask_user`), and granting them widens
   nothing — every discovered invocation still needs its own grant.
4. **The tool-RAG index omitted the new tools** — added to
   `BUILTIN_TOOL_DESCRIPTIONS` and `ALWAYS_AVAILABLE`.
5. **Registry argument bounds were advertised but not enforced** —
   `maxLength`/`maximum` are now validated (increment 5 follow-up closed).

## Evidence classification

- Deterministic focused tests: 618 passed / 0 failed (registry, policy,
  gates, parsing, discovery, file security).
- Live provider calls: ~10 purposeful streaming turns across the two
  protocol families (minimal quota use).
- Real Gateway execution evidence: filesystem file creation + tool frames +
  metrics frames from the real loop.
- Mock protocol integration: covered by the existing deterministic suites
  (not labeled as live).

## M5.2 exit gate assessment

**BLOCKED — one remaining condition.** The canonical tool matrix, protocol
diversity, streaming fidelity, denylist security and discovery/skill
capabilities are validated (see the matrix). What remains for the full exit
gate per the roadmap: a second INDEPENDENT provider endpoint/credential for
at least one family (to demonstrate cross-provider behavior beyond one
aggregator), and read/search/edit/patch/shell completion on a provider whose
approval posture allows headless mutation acceptance (or an attended
approval session). Missing prerequisite: authorized credentials for an
independent second provider. M5.2 stays open until then.
