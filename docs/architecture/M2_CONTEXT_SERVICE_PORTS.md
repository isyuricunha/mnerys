# M2.1 — Chat Context Service Ports

**Status: partial internal implementation.** This is a dependency boundary in the
existing Python application, **not** a standalone Agent Core, a public Gateway
endpoint, durable context store, or a working CLI/TUI transport.

The [roadmap](../ROADMAP.md) and [ordered M2 plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
define the remaining delivery and acceptance gates.

## Current dependency flow

```text
Web /chat or /chat_stream
  -> authenticated session admission (owner and session checked)
  -> HTTP compatibility adapter (legacy message events)
  -> build_chat_context_core(user=admitted owner, ports=...)
       -> ChatContextPorts
            -> LegacyChatContextPorts
                 -> existing handlers, preferences, RAG, uploads and stores
  -> Gateway prompt selection, model routes and existing runtime
```

The [context admission guard](../../src/gateway_context_admission.py) validates
the server-issued admission before preprocessing. Neither caller-provided
JSON nor a fallback candidate grants ownership or authorizes any tool.
The [shared context algorithm](../../routes/chat_helpers.py) receives an
explicit owner; the Web adapter retains the original request identity and
event callback. Existing users of the legacy helper still work.

## Implemented ports

The [`ChatContextPorts` protocol](../../src/gateway_context_ports.py) defines
the following operations. [`LegacyChatContextPorts`](../../routes/chat_helpers.py)
delegates every operation to the *existing* implementations; the adapter does
not create another persistence engine, memory index or RAG pipeline.

| Responsibility | Operations | Existing implementation |
|---|---|---|
| Presets and preprocessing | `extract_preset`, `preprocess_message` | Existing chat handler, including attachment preprocessing |
| User message writes | `append_user_message` | Existing session/history writer |
| Private request-local messages | `append_private_message`, `private_messages` | Existing bounded, process-local incognito transcript |
| Preferences | `load_preferences` | Existing owner-scoped preference loader |
| Attachment manifest | `upload_manifest` | Existing upload handler and path/owner validation |
| Memory, RAG, Web context | `build_preface`, `used_memories` | Existing chat processor and last-used-memory tracking |
| Model-window shaping | `compact`, `trim` | Existing context compactor and token-window trimmer |

The shared algorithm preserves the inherited ordering: preprocess, conditional
message append, optional post-save event, preference/manifest retrieval, prompt
preface, memory and untrusted-source injection, session history, compaction and
trim. The optional event callback is supplied by the adapter, not by a remote
client. When `persist_user_message=False`, no message or event is written.
Incognito context cannot read the persisted session history and is not
converted into persistent storage.

Callers without a port argument continue through the legacy adapter.
Server-side tests can inject another provider to verify the interface without
opening an HTTP connection or using an actual database. Such providers are
**trusted internal code**, not client-specified plug-ins.

## Explicitly not extracted yet

- Model-ID normalization, current-time and YouTube-source helpers, and
  the session's own `get_context_messages` remain linked to the legacy module.
- Memory/RAG and attachment operations are **ports to inherited implementations**,
  not newly independent services or alternative storage.
- The context algorithm itself still lives in `routes/chat_helpers.py`;
  moving it into `src/` requires isolating its remaining dependencies first.
- Process-local incognito records and tool approvals are not durable and do not
  survive worker replacement.
- Authenticated remote transport, turn/event durability, crash recovery and
  multiple real client adapters remain M2 acceptance requirements.

This slice changes **no database schema, authentication, external API, provider
configuration, deployment or client protocol**.

## Validation

The [context-port tests](../../tests/test_mnerys_gateway_context_ports.py)
inject independent in-memory providers and reject accidental direct calls
to legacy preference, attachment, message and compaction functions.
They cover ordinary persisted context, incognito isolation and continuation
without message writes. The [admission tests](../../tests/test_mnerys_gateway_context_assembly.py)
cover owner/session mismatch before side effects.

Local regression: **790 passed**, one existing `pytest-asyncio` fixture-scope
warning. Hosted CI and production-preview acceptance are **not** implied by
local tests. This feature is not considered complete until the remaining
service dependencies and real multi-client acceptance gates are verified.
