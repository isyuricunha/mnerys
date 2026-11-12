# Native Tool Catalog and Approval Continuation

## Scope

This is a corrective increment following M5.3, not a new engine or a
security-policy migration. The single Agent Core/Gateway remains authoritative
for Web, CLI, TUI and future Telegram turns. Chat/Search/Shell composer toggles
no longer govern whether the model may discover a tool.

## Default capability availability

Every agent-capable session receives the **server-authorized** tool inventory,
including Web tools, local file inspection, shell, skills and connected MCP
tools. The existing `ToolSearch` entry is kept in the immediately offered
surface; frequently used read/inspection tools are warmed while all other
authorized tools can be discovered on demand. ToolSearch is a catalog
operation, NOT permission to execute any tool returned by discovery.

Do not load every schema into model context: the runtime's own tool index
continues to select a compact subset. Do not impersonate Anthropic's native
`defer_loading` API on an incompatible provider.

## Web surface

The composer no longer sends `allow_bash`, `allow_web_search` or a
Search-controlled `use_web`. Hidden legacy input elements remain for
backwards-compatible scripts but are not execution authorities.

The admitted Gateway Web intent contract supports
`native_agent_catalog=True`. On this server-controlled path it preserves
account/policy denials, allows registered Web tools to be discovered, and
**never performs unsolicited pre-search**. A genuine request to research on
the Web is routed to the Web tool; explicit "do not search" still wins.
The older opt-in behavior is maintained for its separately tested legacy
adapter and tools-disabled text profile. A selected Web domain cannot erase
the other canonical tool families on admitted Agent turns.

## Approval continuation

An approval is a one-shot control-plane decision that resumes the **sealed**
original request and workspace, without saving a new user message or widening
authority. The approved operation, caller-selected inventory and the allowed
warm discoverable tools are unioned and intersected with actual request
grants and server denials. They are not replaced by a Web-only surface after
an unrelated search result. Auth, cross-owner isolation, exact-operation
approval matching, risk classification, sandboxing, cancellation and replay
checks are unchanged.

The existing failed-tool loop breaker remains bounded to prevent uncontrolled
repeat effects; tools are not fabricated and a failed call is never a success
receipt. After two failed unrelated Web-tool rounds during a confirmed local
inspection request, an admitted Agent gets **one** local-tool rerouting attempt
before the original breaker applies. The reroute never replays effectful calls,
mints grants or broadens tools, and the final response must distinguish a
successful receipt from a task still incomplete. Other exhaustion paths retain
their existing tests and behavior.

## Evidence and remaining gates

Focused suites exercise the cutover, existing Agent Loop fallbacks and
completion guards, approved shell resume, client module graph, static
composer/slash behavior and negative policy tests. Exact production
reproduction also requires an authenticated session using a real configured
provider after a published release; tests alone do not establish that gate.

Official guidance:

- Anthropic: Manage tool context and Tool Search documentation (server-side
  deferred references are provider-specific, not a universal API contract).
- Model Context Protocol: tools/list discovery and authorization requirements.
- Mnerys M5.3 permission and tool-approval architecture.

## Read-only shell navigation

Literal `cd` into the mounted `/workspace` directory or a static relative
subdirectory is classified as low-risk navigation. This fixes harmless
inspection chains such as `cd /workspace && ls -la` that previously prompted
for an unknown executable. It does not widen filesystem access: the real
session CWD, tool authority, process confinement and sensitive-path checks
remain mandatory at execution. Dynamic targets, parent traversal, unknown
absolute destinations and any effectful command in the same chain still
require review under the normal approval policy. YOLO remains separately
opt-in and never overrides underlying authorization.

## Scoped CWD, safe shell redirects and result correlation

The shell runs inside the sandbox at the stable `/workspace` alias. The
approval and session APIs retain the real, server-validated CWD. These are
**two representations of the same workspace**, not two selectable roots.
An ordinary chat bound to a folder without `.git` or `README.md` must
report those absent resources truthfully; it must never silently switch to
an unrelated repository or scan outside its assigned root.

The shell-risk parser recognizes file-descriptor duplication, such as
`2>&1`, without mistaking the ampersand for background execution. It still
classifies every command and every file-write redirection independently,
using the strongest risk result: a safe `git status 2>&1` needs no ordinary
risk approval, but `git push`, `rm`, `>output.txt`, and sensitive-path
writes remain subject to review. Low risk is not execution authority.

Browser-facing Web tool events carry the same model/harness call ID across
start, progress and result. A resumed sealed approval gets a distinct
correlation ID as well. Live and replayed Web tool cards use these IDs;
unknown IDs must not overwrite an unrelated card. The separately typed
CoreToolEvent contract deliberately strips tool arguments, results and
provider correlation IDs. Trusted client-rendering frames preserve the Web
identities; typed Core metadata is not silently widened. Legacy browser
events without IDs retain their single-action compatibility path. Structured
tool receipts and nonzero exit codes remain authoritative over model prose.

## Independent shell inspections after approval

A compound shell command joined with `&&` stops after its first failing
step, as specified by Bash's command-list semantics. The result's nonzero
exit status means the shell action ran and failed; it does not mean the
execution was blocked or that every requested check was performed.

The approval continuation now distinguishes a denied launch from a command
that executed and returned a failure. It preserves bounded observed output
and exit status rather than saying that a partially executed shell did not
run. Only a **verified low-risk, read-only** shell operation with observed
output and no policy/resource error can continue model synthesis for any
remaining independent inspections. It does not replay the approved command,
widen authority, change the session's CWD or retry effectful commands. All
later tool calls remain independently authorized and audited. The Agent
instructions ask for separate calls when results are independently requested.

## Web Agent streaming chronology

The Web adapter forwards the SSE `tool_start`, `tool_progress` and
`tool_output` families in the order emitted by the single Agent Core.
Progress observations are not discarded at the Gateway boundary. Model
narration from a preceding tool round is preserved in its original position
instead of being concatenated into a final assistant bubble.

Agent rounds carry a cumulative model text accumulator for compatibility.
A final plain-text snapshot is reduced to the **last verified post-tool round**
only if that round exists and is a suffix of the cumulative text. The previous
rounds and their tool receipts remain in `round_texts` / `tool_events` for
history replay, and the final content uses `replacement_scope=round`. Explicit
structured `final_response` snapshots keep the original whole-turn
replacement semantics, and ambiguous or absent final rounds are not trimmed.
This makes live rendering and history reconciliation consistent.

The workspace tool description and browser picker must reflect the actual
enforcing containment backend: the selected directory is a CWD, not a grant to
escape required process/filesystem isolation. Missing filesystem entries are
reported as missing; they are never synthesized.

## Tool-output presentation and Markdown code examples

Native tool calls and tool results are distinct, correlated events. The Agent
Core parses and executes actual calls under its authority/containment gates;
the final reply cleanup is **not** a second tool-dispatch pass. Native-mode
final prose therefore preserves literal Markdown code fences such as
```bash examples copied from a README, while explicit tool-call envelopes
(DSML/XML and other supported transports) remain removed. Legacy textual
fenced-tool compatibility retains its separately tested execution behavior.

A real output belongs to the tool receipt in the chronological timeline.
For multi-command inspections the model is instructed to report the observed
status and concise finding for every command, without reprinting long
files/logs already shown. When a model instead repeats almost all of one
large read-only tool output as its entire final message, a conservative
presentation guard replaces **only that duplicate assistant answer** with
actual per-call exit codes. Its matching requires substantial two-way line
overlap, multiple completed read-only calls, and an existing long output;
short excerpts, substantive synthesis, effectful commands and uncertain
receipts are not modified. Raw tool evidence is never deleted or truncated
by this guard, and no further calls are made.

Live tool cards and history replay share one status/output-label formatter
that displays actual `exit N` codes and output line counts. The full content
remains available through the same expandable result card, not copied into
unrelated assistant prose. Approval and error states remain distinct.

This is a corrective presentation increment, not a new Agent engine or
permission mechanism. Authenticating to the Web preview and exercising a
configured model is still required for end-to-end confirmation.

## PT-BR local inspection and truthful completion

Gateway request admission recognizes explicitly phrased PT-BR read-only shell
inspection (for example `whoami`, `hostname`, `uname`, `date`) and
workspace-file discovery (`Glob`, `Grep`, `Read`) as the same local-read
capability class used by equivalent English commands. An explicit request
selects only the needed read/tool family; no model text, tool stdout or
session history grants new operations. In Portuguese `todos os arquivos`,
`todos` is a determiner rather than an English to-do noun and must not
select the Tasks product. Every call still passes independently through
owner/session binding, request authorization, sandbox, resource identity,
risk classification, policy and audit. Mixed edits and negatives are not
reclassified as read-only.

The risk classifier identifies the exact Git inspection
`git branch --show-current` as low-risk, including a harmless `echo` of
its exit code. Branch creation/deletion/rename and compound commands with
side effects still require review. This classification only suppresses
ordinary approval; it never supplies missing operation authority.

A read-only multi-command inspection can finish with an incomplete provider
answer (e.g. only the last command's stdout), or with the model quoting a
blocked duplicate inspection. A bounded, provider-neutral finalizer then
uses previously **executed** read-only receipts to report actual exit codes
and concise observed output. It does not run any command, claim that a
blocked/unknown call succeeded, erase errors, or reproduce long file output.
Server-authored repeated-read policy is retained in the persisted tool
event so an exact duplicate can be distinguished from the original
successful receipt. This fallback runs only for explicit, observed
multi-step read-inspection requests whose final answer is provably
incomplete; a legitimate synthesis remains untouched.

No secrets, private endpoints, server identifiers or real user transcripts
should be added to published documentation.
