# M5.2 — Canonical Tool Registry (Increment 1)

Status: implemented and regression-covered (increment 1). Remaining M5.2
increments are listed at the end.

## What this is

One canonical, provider-independent tool registry — `src/tool_registry.py` —
that owns the model-visible tool vocabulary for every supported model and
provider. The vocabulary follows Claude Code's tool naming and behavior
conventions:

`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `WebFetch`, `WebSearch`

`ToolSearch` and `Skill` are reserved names in the registry but are **not
implemented yet**; they are never advertised and any call naming them is
refused with `tool_unavailable` — never simulated.

## Layer boundaries (unchanged security architecture)

The registry owns **names and argument contracts only**. It never executes,
never checks authority, and never touches the network.

```
model request ──▶ Gateway admission ──▶ turn contract (handler ids)
                                            │
                                            ▼
                          agent_loop builds schemas (handler ids)
                                            │
                                            ▼ wire boundary (llm_core)
                    project_tools_for_wire / project_messages_for_wire
                                            │
                                            ▼
                    provider sees canonical names, canonical args
                                            │
                                            ▼ inbound native tool_calls
                     _resolve_registry_native_call (agent_loop)
                        validated + translated exactly once
                                            │
                                            ▼
              ToolBlock(handler_id, internal-encoded content)
                                            │
              ▼ authority · approval fingerprints · resource bindings ·
                dispatch (all keyed on handler ids, unchanged)
```

- **Internal identity** remains the historical handler id
  (`read_file`, `bash`, `write_file`, …). Approval fingerprints, exact
  operations, resource bindings, dedup signatures, UI events, transcripts and
  the whole execution engine are keyed on it and did not change.
- **Model-visible identity** is the canonical Claude Code name. The projection
  is transport-only and happens at the wire boundary in `_stream_llm_inner`
  (and, for history-only, in `llm_call_async` / `llm_call` / request
  snapshots).
- The bijection canonical ↔ handler is unique and every canonical tool maps to
  a real handler tag; `project_tools_for_wire` never adds a tool the turn did
  not admit, and MCP tools pass through untouched with their server-defined
  names and schemas.

## Inbound governance (the new boundary)

`_resolve_registry_native_call` validates every model-generated native tool
call **before every other guard**:

- Canonical exact-case name → `resolve_model_call` validates required
  parameters, types, unknown arguments, unsupported arguments, and translates
  arguments to the internal handler form once. The call is then rewritten to
  `(handler_id, internal args)` so downstream checks (offered/declared
  filtering, conversion, authority, dispatch) behave exactly as for a legacy
  native call.
- Legacy built-in spellings and inherited aliases (`read_file`, `shell`,
  `websearch`, …) → rejected as `legacy_tool_name` with the canonical name in
  the error. Historical records and handler-keyed approvals are unaffected —
  rejection applies only to **new model-generated calls**.
- Unavailable names (`ToolSearch`, `Skill`) → rejected as `tool_unavailable`.
- Unknown/unsupported arguments → rejected as `invalid_tool_arguments` with an
  actionable message; arguments are never silently dropped.
- Non-registry tools (inherited tools, MCP, request-scoped external schemas)
  are untouched.

**Governance follows the advertised surface.** The gate applies only when the
turn actually offered registry tools (`offered_tool_names ∩ HANDLER_IDS ≠ ∅`).
Turns with no declared registry tool — no-schema finetune routes, textual-only
transports, MCP-only offers — keep their inherited vocabulary; rejecting their
only channel would silently remove functionality. The textual/fenced channel
of local models likewise keeps its legacy vocabulary and content encodings in
this increment (documented boundary below).

**Sealed approval replay is exempt.** A resumed turn re-emitting the exact
sealed identity `(pending.tool_name, pending.content)` is not a fresh model
call; it travels the normal execution path where the sealed authority,
redundancy guards and replay prevention apply exactly as before.

## Rejection feedback (model recovery)

A rejected call becomes an inert `tool_call_rejected` record block. It is
never routed, never budget-counted, never dispatched:

- The main agent loop skips it before `propose_action`, routing, approvals and
  the dispatcher, emits a truthful `tool.blocked`/`tool_output` event with
  `execution_attempted: false`, and produces the aligned tool result so the
  continuation history echoes the model's own call plus the accurate error.
- `execute_tool_block` refuses such blocks as defense in depth for every other
  executor.
- The resolution audit understands the canonical↔handler equivalence, so an
  accepted canonical call is not reported as a rejected request, while a
  genuinely rejected call is.

Errors are phrased to teach the canonical name, e.g.
`'bash' is not a tool name; use Bash. Available built-in tools: Read, Write,
Edit, Glob, Grep, Bash, WebFetch, WebSearch.` — the model can recover by
re-issuing a valid canonical call.

## Provider adapters

- Harmony/gpt-oss aliasing now protects canonical names explicitly and aliases
  only inherited non-registry colliding tools (`python`, `browser`). The old
  `bash → run_shell_command` alias is gone: the collision was case-sensitive
  (`bash` built-in vs our schema name), and the canonical `Bash` no longer
  collides. Whether gpt-oss accepts a custom tool literally named `Bash` is
  verified by the real-provider M5.2 exit-gate tests.
- Anthropic wire compatibility verified against the current official docs:
  custom tool names must match `^[a-zA-Z0-9_-]{1,128}$` (uppercase letters
  allowed) and tools carry an `input_schema` JSON-Schema object — exactly the
  shape the registry projects.
- The canonical registry (Claude Code tool conventions) is deliberately
  distinct from Anthropic's versioned API-native tool schemas
  (`bash_20250124`, `text_editor_20250124`, server tools, client toolsets):
  those are transport-level dated `type` declarations, not our client tool
  vocabulary, and are not used here.

## Honest schemas

Every advertised argument must be honored by the existing handler:

- Argument keys differ from Claude Code only where the internal handler
  contract differs (`Read.file_path` → internal `path`,
  `Grep.-i` → `ignore_case`, `Grep.head_limit` → `max_results`, …); the
  translation is bidirectional and round-trip safe.
- Annotation-only parameters (e.g. `Bash.description`, `WebFetch.prompt`) are
  accepted but never forwarded; their wire descriptions disclose that.
- Mnerys-extension parameters (`WebFetch.full`, `WebFetch.query`,
  `WebSearch.time_filter`) are marked as same-on-every-provider extensions.
- Unsupported Claude Code parameters (`Bash.timeout`,
  `Grep.output_mode`, batched `WebFetch.urls`) are rejected explicitly instead
  of being simulated.
- Empty `Write.content` is legitimate (creates an empty file); empty required
  paths are not.

## Increment-1 boundaries (intentional)

1. **Textual/fenced channel of local models** keeps the legacy vocabulary
   (```bash fences, fenced tool tags, misfenced lookups). It is their only
   channel, no schemas are advertised to them, and the fence tag is a
   language/content classifier whose case is folded — a name-level
   canonical/legacy distinction does not exist there. Canonical names in
   explicit textual markup (`<invoke name="Read">`, JSON envelopes) DO resolve
   through the registry.
2. **Prompt vocabulary** follows each route's advertised surface. Finetuned
   textual routes (e.g. the qwen38 tool-router) keep their trained vocabulary;
   schema-declaring routes rely on the canonical wire schemas. A full
   prompt-vocabulary migration is increment-2 work.
3. **Compact preview loop** (`clean_agent_preview.py`) builds its own provider
   requests and remains on a self-consistent legacy surface this increment;
   migrating its transport and policy layer to canonical names is increment-2
   work.
4. **Read-before-edit enforcement for existing files** is the next M5.2
   increment; current safeguards are preserved, nothing was weakened.

## Tests

- `tests/test_tool_registry.py` — catalog immutability, bijection, strict
  schemas, validation matrix, legacy/alias/unavailable rejection, projection
  (including MCP pass-through and no-additions), history round-trip,
  rejection records.
- `tests/test_tool_registry_integration.py` — native-channel governance,
  rejection feedback and audit awareness, dispatcher refusal of rejected
  records, converter/XML-invoke canonical support, canonical request
  snapshots.
- `tests/test_harmony_tool_aliasing.py` — canonical names never renamed;
  inherited colliding tools still aliased; reverse mapping updated.
- Existing suites cover approval identity (`test_approved_replay_message_shape`,
  gateway approval suites), sealed-approval resume
  (`test_mnerys_gateway_filesystem_approvals`), duplicate-dispatch dedup and
  continuation (`test_agent_rounds_exhausted`), and tool-result threading.
