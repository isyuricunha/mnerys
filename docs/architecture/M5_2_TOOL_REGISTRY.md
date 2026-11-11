# M5.2 — Canonical Tool Registry (Increment 1)

Status: increments 1 (canonical registry), 2 (read-before-edit), 3 (unified
textual/native tool vocabulary), 4 (derived-artifact writer publication
boundary) and 5 (ToolSearch and Skill) implemented and regression-covered.
Remaining M5.2 work is listed at the end.

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
   the derived-artifact writers were its remaining gap and are now covered
   by increment 4's publication boundary. The filesystem security redesign
   beyond it (streaming/large-file contracts) remains follow-up work.

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
race that the digest check only narrows), background jobs launched by bash
(invalidated at launch; later writes are the same residual class), and the
TUI host bridge (client-side filesystem; server baselines do not apply
there). The derived-artifact writers were the remaining gap and are now
covered by increment 4's publication boundary below. No universal "no edit
without read" claim is made beyond these boundaries.

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
- `tests/test_derived_artifact_safety.py` — the increment-4 publication
  contract: exclusive new-artifact publication and appeared-destination
  refusal (including the deterministic link race), symlink and directory
  destinations refused with targets untouched, replace-after-read with
  re-baselining, stale/foreign/forged/opaque/oversized refusals, permission
  preservation, held-flock serialization, staged-file cleanup, ImageMagick
  SVG render publication, real-ffmpeg still/batch/sidecar exports, and the
  transcribe_media user-file versus internal-cache classes (fake Whisper
  decoder, real publication path).

## Increment 4 — derived-artifact writer publication boundary (implemented)

`src/agent_tools/artifact_publication.py` extends the increment-2 contract to
the derived-artifact writers. Generators no longer write destination paths
directly (ffmpeg `-y`, `Path.write_text`); they write a staged file beside the
destination and publication is one authorized, atomic step.

**Audited writers and their classifications.**

| Writer | Destination | Class |
| --- | --- | --- |
| `inspect_media` SVG render (`output_path`) | workspace PNG | `user_file` |
| `inspect_media` still export (`output_path` + `timestamp`) | workspace image | `user_file` |
| `inspect_media` batch stills (`exports[…]`) | workspace images | `user_file` |
| `inspect_media` clip export (`output_path` + start/end) | workspace video | `user_file` |
| `inspect_media` timestamp sidecar (`timestamp_path`) | workspace text | `user_file` |
| `inspect_media` preview frames | `TemporaryDirectory` | internal, never published |
| `transcribe_media` explicit `output_path` | workspace text/subtitle | `user_file` |
| `transcribe_media` automatic transcript | `.odysseus/transcripts/…` | `internal_cache` |
| `generate_image` / `edit_image` | `GENERATED_IMAGES_DIR/<uuid>.png` | server-managed new artifact (exclusive creation; collision refused) |
| `todowrite` | `DATA_DIR/agent_todos/<session>.json` | internal state, not a workspace file |
| email `download_attachment` | hash-scoped extraction dirs | server-managed new artifacts |
| upload media ingress, background-job logs | server temp/data dirs | server-managed |

**Existing-file versus new-artifact semantics.** Replacing an existing
user-visible file requires a real, server-recorded read (same owner and
session) with unchanged identity and content digest — validated through the
same `read_baselines` store, so TTL expiry, eviction, code-execution
invalidation, and restart clearing apply identically. Because the new content
is generated rather than derived from the old bytes, line-scope anchoring does
not apply; what is refused is any absence of awareness: no baseline,
`opaque` (structured-document) observations, files above the baseline size
bound (fail-closed), and stale/foreign/forged baselines all refuse with an
actionable `read_before_edit` result before any byte changes. Creating a NEW
artifact needs no prior read; publication is exclusive (`os.link` with
`EEXIST` refusal), so a destination that appears while the generator runs is
never overwritten.

**Exact enforcement boundary.** The destination's parent directory is opened
with `O_NOFOLLOW` on its final component; the destination is always addressed
through that directory fd and refused when it is a symlink or non-regular
file — a symlink is never followed or written through. The existing
destination is validated on a descriptor holding an exclusive `flock`
(cooperating writers of this server are mutually excluded), the staged file
inherits the existing file's permissions, the path is re-opened immediately
before publication and must still resolve to the validated inode, and the
publication itself is an atomic `os.replace` within the same directory.
After a successful user-file publication the server re-baselines the exact
bytes it published. Byte-identical recreate (inode reused, same digest) is
content-safe by the same definition the text tools document.

**Concurrency, cancellation, and partial failure.** Batch exports pre-check
every destination (and the sidecar) before any generator runs, so an
avoidable refusal never strands partial output; a mid-batch ffmpeg failure
reports the already-created files and the failed destination honestly.
Staged files live in the destination directory under `.odysseus-publish-`
(hidden, disposable) and are removed on refusal, failure, and context exit.
A hard process kill can leak a staged file — the same documented class as
the text tools' staging — and it is never a user-visible artifact name.

**Residual limitations (unchanged classes, now narrower).** Non-cooperating
writers (a running shell command, a background job writing later) can still
mutate files outside any tool call; the digest re-verification narrows but
cannot eliminate that race, and the bash/python invalidation boundary (every
owner/session baseline dropped after code execution) remains the honest
guarantee — a derived overwrite after shell execution requires a fresh read.
The TUI host bridge executes on the client host; server baselines do not
apply there. `flock` excludes cooperating writers only; the atomic rename
does not make compare-and-write universal.

**generate_image / edit_image.** These never accept model-chosen
destinations: they write server-generated UUID names into the gallery
directory, so overwrite is impossible by construction; creation is now
exclusive (`O_CREAT | O_EXCL`) so even a UUID collision refuses instead of
clobbering. Ownership registration and gallery records, retrieval routes,
and client preview behavior are unchanged.

## Increment 5 — ToolSearch and Skill (implemented)

The canonical catalog is now ten tools: `Read`, `Write`, `Edit`, `Glob`,
`Grep`, `Bash`, `WebFetch`, `WebSearch`, `ToolSearch`, `Skill`. Both new
tools are real registry-backed implementations (`src/agent_tools/discovery_tools.py`,
handlers `tool_search`/`activate_skill`) that surface what already exists and
is already authorized through the single Gateway path — no provider aliases,
no second registry, no simulated discovery.

**ToolSearch contract.** `ToolSearch(pattern: string ≤200 chars, limit?: integer 1–20, default 5)`.
`pattern` is a case-insensitive Python `re.search()` pattern over the same
search space Anthropic's Tool Search documents: tool names, descriptions,
argument names and argument descriptions. Results are ranked deterministically
(tool-name hit → argument-name hit → description-only hit, then shorter names,
then alphabetical) and bounded. Every result carries the model-visible name
(canonical projection; MCP tools keep their original `mcp__<server>__<tool>`
identities), source (`builtin`/`inherited`/`mcp:<server>`), description and
argument schema. Invalid regexes, empty patterns, wrong argument types and
unsupported arguments return truthful errors.

**Discovery source and snapshot consistency.** ToolSearch ranks ONLY over the
discovery snapshot frozen in the bound turn contract
(`TurnContract.discoverable_schema_json`): every policy-admitted schema of the
turn, offered or not, resolved once by the server before inference. Within a
turn the snapshot is immutable, so discovery is consistent; a tool hidden or
denied by the active policy (disabled names, guide-only, `disable_mcp`,
owner-blocked connections) never enters the snapshot and cannot leak its
privileged schema, configuration or metadata through search. A tool does not
become discoverable because its name appears in model output, user text or a
historical transcript — discovery reads server inventory only. With no bound
contract (plain preview/recovery paths) the search falls back to the registry's
real built-ins and activation is refused.

**Discovery is not authorization.** Activation is server code, never model
output: `TurnContract.activate_discovered_tools` widens the model-visible
`offered` set for the remainder of the turn only for tools already in the
turn's `executable` set and snapshot; `required`, `executable`, the policy, the
request authority and approval rules are untouched, and the original contract
binding is restored when the turn ends. The agent loop reads the LIVE contract
when assembling each round's wire surface, so a discovered tool is genuinely
invocable (schema visible, dispatch permitted) instead of silently claimed.
A discovered invocation must STILL pass request-authority grants, exact
approval, resource binding and audit — discovery alone refuses with truthful
feedback. Discovery calls are audited through the normal tool-event path.

**Skill contract.** `Skill(name?: string ≤80 chars, path?: string ≤200 chars)`.
With no arguments it returns the compact, cache-friendly eligible index (the
same owner-scoped, approval-gated list the system prompt carries — lazy: never
skill bodies). With `name` it activates one installed skill from the existing
SKILL.md library (`services.memory.skills.SkillsManager`, `data/skills/…`,
Agent-Skills-style frontmatter) and loads its bounded instructions
(≤32,000 chars) with provenance (storage, owner scope, status); `path` reads
one supporting reference file confined to the skill's own directory
(≤16,000 chars returned, ≤1 MB underlying file). Missing skills, foreign-owner
skills (denied without leaking metadata), drafts and failed/low-confidence
audits refuse truthfully at the shared `activation_eligible` gate — the same
gate the loop's relevance injection applies, extracted to a single enforcement
point in `services/memory/skills.py`.

**Skill content is untrusted.** Activation changes no policy, grants no
permission, disables no approval and executes nothing: a skill's scripts or
assets run only through normal authorized tools and permissions, referenced
files cannot escape the skill directory (symlink and traversal refused through
the same confinement the read uses), and skill instructions never become
privileged system policy — they are candidate procedures the model applies
through already-authorized tools.

**Compatibility.** Both tools flow through the shared schema projection,
textual parsing (canonical fenced calls validate and convert exactly like
native calls), native calling, compact preview and the Web/CLI/TUI paths.
MCP names are unchanged; the qwen3.8 carve-out is untouched (schemas are
withheld for that router exactly as before).

**Residual limitations and M5.5 dependencies.** Skill scopes are owner +
built-in today (project/session scopes arrive with M5.5's skill management);
there is no hot reload, versioning, publication flow or marketplace; MCP tool
schemas in discovery come from the manager's connected-server inventory at
admission time (M5.5 owns refresh notifications and versioned catalog
snapshots); regex search cost is bounded by the ≤200-char pattern and the
admitted inventory size.

## Remaining M5.2 work (after increment 5)

1. **The qwen3.8 fine-tuned router compatibility carve-out** — the route keeps
   `legacy_compat=True` until its trained weights are refreshed to the
   canonical vocabulary; migration is a fine-tune-cycle task, not a code
   alias (the carve-out is one parameter at one parse site and cannot widen
   permissions). Increment 4 verified the route: derived-writer enforcement
   lives in the handlers, not parsing, so the carve-out behavior is
   unchanged; a future canonical-vocabulary fine-tune needs no code change
   beyond dropping the one `legacy_compat` flag.
2. **Real-provider exit-gate matrix** — read/search/edit/patch/shell on at
   least two provider API protocols (the exit gate below), which has not run
   against live providers yet.
3. **Filesystem security redesign follow-ups** — streaming/large-file
   contracts for the baseline (oversized existing destinations are refused
   fail-closed today), concurrent-shell residual race narrowing, and
   background-job write windows after launch invalidation.
4. **Canonical-spelling denylist translation** — denylist entries written in
   canonical spellings (e.g. `Read`) do not yet translate to handler ids in
   every gate; the settings surfaces write handler ids today. Track the
   full alias-set translation for a follow-up.
