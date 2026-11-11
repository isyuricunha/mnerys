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

No secrets, private endpoints, server identifiers or real user transcripts
should be added to published documentation.
