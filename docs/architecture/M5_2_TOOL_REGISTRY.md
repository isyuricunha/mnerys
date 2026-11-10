# M5.2 — Canonical Tool Registry (Increment 1)

Status: increments 1 (canonical registry), 2 (read-before-edit) and 3 (unified
textual/native tool vocabulary) implemented and regression-covered. Remaining
M5.2 work is listed at the end.

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
Turns with no declared registry tool — MCP-only offers — keep their inherited
vocabulary; rejecting their only channel would silently remove functionality.
Since increment 3 the textual/fenced channel is governed by the same registry
(see below), with one documented compatibility carve-out for the fine-tuned
router trained on the legacy vocabulary.

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

## Increment-1 boundaries (historical)

1. **Textual/fenced channel of local models** kept the legacy vocabulary in
   increment 1 (```bash fences, fenced tool tags, misfenced lookups). Increment
   3 lifted this boundary: canonical names are executable in every textual
   format and legacy spellings are refused on governed turns (see below).
   Canonical names in explicit textual markup (`<invoke name="Read">`, JSON
   envelopes) already resolved through the registry in increment 1.
2. **Read-before-edit enforcement** is increment 2 (implemented above);
   the filesystem security redesign beyond it (streaming/large-file
   contracts, derived-artifact writers) remains follow-up work.

## Increment 3 — unified tool vocabulary across textual and native execution (implemented)

Every supported model execution path now shares the canonical Claude Code
tool contract. `src/tool_registry.py` remains the single source of truth; no
second registry, duplicated execution implementation or provider-specific
alias set was added.

**One governed textual admission path.** All textual tool-call formats —
fenced ```Tool blocks (canonical and legacy tags), `<invoke>` DSML, direct XML
tags, Qwen/Hermes JSON envelopes, StepFun tokens, MiniMax `tool_code`, Gemma
call blocks, `function_model` envelopes, `[TOOL_CALL]` blocks, raw OpenAI JSON
leaks, prose web-JSON recovery, and the Qwen3 MLX adapter — classify the
model-written name through the registry on every governed turn:

- Exact canonical spelling (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`,
  `WebFetch`, `WebSearch`) resolves through `resolve_model_call` exactly like
  a native call — same names, same argument contracts, same repair and
  rejection behavior. `textual_function_call_to_tool_block`
  (`src/tool_schemas.py`) is the shared converter; `parse_tool_blocks(...,
  legacy_compat=False)` is the governed entry point.
- Legacy handler ids and inherited alias spellings of registry tools
  (`read_file`, `bash`, `websearch`, …) become inert `tool_call_rejected`
  records with the canonical name in the error — never silently executed.
- Case-variant spellings are refused in both modes; they were never a valid
  form in any channel.
- Unavailable Claude Code names (`ToolSearch`, `Skill`) get a truthful
  `tool_unavailable` refusal record in every textual channel — never
  simulated, never silently dropped on governed turns.
- Non-registry tools (inherited capabilities, MCP, request-scoped declared
  schemas) keep their historical conversion, including the lowercase mapping
  models already rely on. Genuine inherited capabilities outside the registry
  are preserved.

**Compatibility carve-out (one route, documented).** The qwen3.8 fine-tuned
tool router has no schemas and was trained on the legacy vocabulary; its
textual turns keep `legacy_compat=True`, under which exact legacy spellings
keep working while canonical spellings also resolve (adoption is purely
additive). Every other textual route is governed. The carve-out lives at the
single stream-agent-loop parse site; it cannot widen permissions or bypass
any dispatcher guard because rejected records never reach dispatch.

**Textual channels carry the canonical argument contract.** XML-style
channels transport every value as text, so string-typed integer/boolean
values are coerced to the registry's declared types before validation
(`_registry_typed_arguments`); unambiguous single raw bodies follow the same
canonical raw mapping the canonical fences use (passthrough for `Bash`/
`Read`/`WebSearch`, named argument for `WebFetch`/`Glob`/`Grep`, explicit
refusal for `Write`/`Edit` raw bodies). Unknown names, unknown arguments and
malformed arguments are rejected with feedback instead of being dropped or
guessed.

**Prompt vocabulary is canonical.** `TOOL_SECTIONS`, the domain rules, the
uploaded-file read hint, web follow-up directives and recovery/retry
instructions all teach the canonical names and argument keys
(`Read`/`Write`/`Edit`/`Bash`/`WebSearch`/`WebFetch`, `file_path`/
`content`). Illustrative fences in prose are the same executable channel as
before — a textual model writing ```Read executes it as a tool call, which is
the contract.

**Compact preview canonicalization.** The compact preview builds its own
provider requests; its wire copy now carries the canonical vocabulary and its
inbound calls are registry-governed:

- `_canonical_wire_request` projects the round request's schemas, prior
  tool-call history and forced `tool_choice` to canonical names at the
  preview's own send boundary (`project_tool_choice_for_wire` keeps forced
  constraints matching the projected schema names). The internal policy,
  dedup and dispatch state stays handler-keyed; the wire projection is
  idempotent with llm_core's boundary and does not churn cacheable prefixes.
- `_unproject_preview_tool_calls` maps returned canonical calls back onto the
  handler-keyed execution contracts exactly once, before any policy or
  dispatch step, using `resolve_model_call`; offered handler-keyed names pass
  through unchanged; MCP and inherited tools keep their identity.
- A call the registry rejects is sealed on the call (`_registry_rejection`),
  stripped from the persisted history by `protocol_safe_tool_calls`, and
  answered with a truthful rejection result (`blocked: true`,
  `execution_attempted: false`) — no false execution status, no dispatch.
- Historical transcripts stay compatible: persisted messages keep their
  internal identities, and rendering of legacy histories is unchanged.

**Increment-2 contract hardening included here.** Two increment-2 gaps found
by the regression matrix were fixed: new-file `write_file` creation now
re-baselines the content it just wrote (so create→overwrite and
create→edit flows keep working without a redundant reread, matching every
other successful mutation), and `apply_patch` restored the documented
universal-newline reading (CRLF files patch again; the validate-then-move
digest still guards the raw bytes).

## Increment 2 — server-verified read-before-edit (implemented)

`src/agent_runtime/read_baselines.py` adds the second M5.2 layer: a registry
file-mutation tool — `write_file`, `edit_file`, `apply_patch` — may mutate an
EXISTING file only when the same owner and session recorded a real,
successful server-side `Read` of that exact file, and the file still matches
what was observed.

**Baseline binding.** A baseline is pure server state, keyed
`(owner, session_id, resolved path)`: the file identity (device, inode via
`FileObjectIdentity`), the full-content SHA-256, size, mtime_ns, and the
observation shape — `FULL` (whole file), `LINES a-b` (a range or truncated
read), or `OPAQUE` (derived document text, authorizes nothing). The model
never sees or supplies a version token, so there is nothing to forge. A
partial read authorizes an anchored edit only when every occurrence of the
anchor text lies inside the observed lines, and never authorizes a full-file
overwrite of unread content.

**Lifecycle.** Invalid on: TTL expiry (1h), store eviction (2048 entries,
oldest first), file replacement (identity change — delete-and-recreate,
rename-swap, symlink swap), content change (digest mismatch), code execution
(`bash`/`python` invalidate every baseline of the owner/session — see the
boundary below), and process restart (in-memory store, matching the approval
store; a restart is a new incarnation). Permission/resolution changes refuse
the mutation before the baseline check is ever reached (confinement runs
first).

**Validate-then-mutate sequence.** The mutating tools open the target through
the parent directory fd with `O_NOFOLLOW` on the final component (a symlink
swap of the file is refused, not followed), take an exclusive `flock` on that
descriptor, and validate identity + digest on the SAME fd they rewrite in
place — so a path swap after the open cannot redirect the write, cooperating
writers (this server's tools, any process, any thread) are mutually excluded,
and ownership/permissions/identity are preserved by construction (same
inode). After a successful mutation the tool re-baselines the exact content
it wrote (server-observed), so read→edit→edit flows work without redundant
rereads. `apply_patch` re-verifies every target's digest immediately before
its staged replaces and aborts the whole patch on mismatch (existing backup
rollback unchanged). New-file creation keeps the exclusive semantics: a file
that appears during creation is never overwritten — the model is told to Read
it first.

**Failure semantics.** Every refusal is a `ReadBaselineError` with an
actionable message (`No recorded read…`, `changed since it was read… Read it
again`, `replaced or recreated…`, `only partially read…`, `outside the lines
observed…`) and the result carries `read_before_edit: true`. Refusals happen
after admission/authority/approval gates and BEFORE any byte is written;
results report `mutation_attempted` honestly when a mid-write failure left
partial bytes, and such content no longer matches any baseline — the next
edit is refused until the agent rereads.

**Enforcement boundary (the honest guarantee).** The contract covers the
registry's file-mutation tools. It does NOT cover: contained `bash`/`python`
execution (the sandbox's writable `/workspace` surface can rewrite files with
no tool call — mitigated by invalidating every session baseline after code
execution, converting the bypass into a visible reread requirement, not
hidden; a shell command running CONCURRENTLY with an edit remains a residual
race that the digest check only narrows), derived-artifact writers
(`inspect_media`/`generate_image` outputs — follow-up), background jobs
launched by bash (invalidated at launch; later writes are the same residual
class), and the TUI host bridge (client-side filesystem; server baselines do
not apply there). No universal "no edit without read" claim is made beyond
these boundaries.

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
- `tests/test_read_before_edit.py` — the increment-2 contract: read→edit,
  edit-without-read, wrong file, partial-vs-full replacement, modification
  and replacement after reading, owner/session isolation, tampered and
  expired baselines, store bounds, cross-process flock serialization
  (deterministic marker + held lock), symlink swap refusal, traversal shapes,
  exclusive creation and appeared-file races, mid-write corruption handling,
  the bash invalidation boundary with the demonstrated sandbox write surface,
  and structured-patch behavior.
- `tests/test_textual_canonical_tool_calls.py` — the increment-3 textual
  contract: canonical fenced/XML/JSON/token/leak/prose parsing through the
  registry, exact-case discipline, legacy-name and unavailable-name refusal
  records in both modes, malformed-argument feedback, misfenced rescues,
  native/textual block parity, rejected records refusing execution, and
  read-before-edit protection through textual canonical calls.
- `tests/test_preview_canonical_tool_representation.py` — the compact-preview
  canonicalization: canonical wire request projection (schemas, history,
  tool_choice), handler-keyed unprojection, sealed registry rejections with
  truthful results, and unchanged identity for inherited/MCP calls.
- `tests/test_agent_evidence_loop.py` — governed textual calls executing
  end-to-end (canonical fences and `<invoke>` calls produce real artifacts),
  read-before-edit intact through the loop.
- `tests/test_workspace_confine.py`, `tests/test_effect_resource_bindings.py`
  — confinement and effect bindings under the baseline contract (updated to
  the increment-2 read-before-edit flow; apply_patch universal-newline
  reading covered).

## Remaining M5.2 work (after increment 3)

1. **`ToolSearch`/`Skill` implementations** — discovery and activation of
   installed skills/MCP tools; reserved names stay refused until then.
2. **Derived-artifact writer contracts** (`inspect_media`/`generate_image`
   outputs) for the read-before-edit baseline.
3. **The qwen3.8 fine-tuned router compatibility carve-out** — the route keeps
   `legacy_compat=True` until its trained weights are refreshed to the
   canonical vocabulary; migration is a fine-tune-cycle task, not a code
   alias (the carve-out is one parameter at one parse site and cannot widen
   permissions).
4. **Real-provider exit-gate matrix** — read/search/edit/patch/shell on at
   least two provider API protocols (the exit gate below), which has not run
   against live providers yet.
5. **Filesystem security redesign follow-ups** — streaming/large-file
   contracts for the baseline, concurrent-shell residual race narrowing.
