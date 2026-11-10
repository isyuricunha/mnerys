# M5.1 Architecture & Acceptance: Recoverable Run State & Fast Development Loop

**Status: COMPLETE (November 10, 2026).** All M5.1 requirements and exit gates are verified. The contract-based affected test selector (`scripts/dev/affected_tests.py`) is validated and active. Durable turn/event/approval recovery across restarts, disconnects, and process interruptions is fully implemented and tested with zero reconstructed authority from history.

---

## 1. Scope and Core Invariants

M5.1 establishes two permanent capabilities:
1. **Fast, conservative local development validation:** A deterministic affected-test selector maps imports, source-reading fixtures, and semantic contracts to run only impacted tests locally while preserving full CI matrix execution on GitHub.
2. **Durable, restart-safe state recovery without authority reconstruction:**
   - Durable audit records accurately capture execution state (`running`, `completed`, `failed`, `cancelled`, `awaiting_approval`, `approval_decided`, `interrupted`).
   - Interrupted runs always record `{"code": "interrupted", "effects": "unknown", "retryable": False}`. Operations with uncertain side effects are **never automatically replayed or re-executed**.
   - No executable authority is ever reconstructed from UI cards, stored transcripts, or historical approval records. Approval decisions grant authority only within the exact live in-memory session incarnation.
   - Pending approvals on interrupted runs are retired as `superseded`. Expired, denied, and consumed approvals remain permanently terminal.
   - Strict owner and session-incarnation isolation: foreign principals receive 404 Not Found; crossed incarnation resumes are rejected.

---

## 2. Key Components

### 2.1 Web Run Journaling (`src/gateway_web_recovery.py`)
- `WebRunJournal` wraps admitted Web SSE streams to produce a metadata-only audit journal.
- Records `tool.started`, `tool.completed`, and `turn.awaiting_approval` without persisting sensitive payload arguments or raw executable commands.
- Atomically finalizes the turn record upon stream draining (`completed`, `awaiting_approval`, `cancelled`, or `failed` with unknown effects).

### 2.2 Atomic Turn Settlement & Records (`src/gateway_turn_records.py`)
- `finalize_with_event`: Atomically updates turn status and appends the corresponding terminal event (`turn.completed`, `turn.cancelled`, `turn.failed`, `turn.awaiting_approval`, `turn.approval_decided`) in a single database transaction. Concurrent settlement attempts settle exactly once.
- `interrupt_stale_turns`: Startup reconciliation query marking all pre-boot `running` turns as `interrupted` and appending `turn.failed` with `{"code": "interrupted", "effects": "unknown", "retryable": False}`. Retires pending approvals as `superseded`.
- `settle_web_approval`: Atomically settles an approval proposal (`consumed` or `denied`) and links the turn state without leaking execution authority.
- `read_web_turn`: Reads audit metadata bound to the specific `owner`, `session_id`, and `incarnation_id`.

### 2.3 Process Reaper Reconciliation (`src/process_reaper.py`)
- On startup, `reap_stale_turn_records()` invokes `SqlTurnRecords.interrupt_stale_turns(before=_PROCESS_STARTED_AT)` to cleanly reconcile dead in-process runs from previous server instances.

### 2.4 Authority Fencing & History Protection (`core/models.py`, `routes/history/history_routes.py`)
- Removed `_history_grants_chat_session_approval`. Persisted history messages, signed cards, and copied metadata cannot grant session or tool execution authority.
- Live authority is bound strictly to `_approval_binding == (session.id, session.owner, session.incarnation_id)`.
- Historical approval projections (`_project_history_approvals`) project cards without active seals as `{"kind": "tool_approval", "resolved": "unavailable"}` and strip signatures to prevent replay or spoofing.

### 2.5 Client Recovery Handling
- **Web UI (`static/js/chat.js`, `static/js/chatRenderer.js`):** Inspects `X-Mnerys-Recovery-State`. Interrupted turns display a non-intrusive warning: *"Turn interrupted. Unconfirmed tool effects are unknown."* Historical unavailable approvals render as *"approval unavailable"*. Avoids duplicate message insertion on metadata-only resume.
- **TUI (`packages/mnerys-cli`):** `TurnTracker` and `TurnPump` track `approval_decided` and `interrupted` turns, safely clearing pending approval prompts and returning control to the composer.

---

## 3. Exit Evidence Matrix

| Area | Contract / Invariant | Test Evidence |
| --- | --- | --- |
| **Development Loop** | Conservative affected test selection | `tests/test_affected_tests.py`: AST dependency traversal, contract mappings, failure on unmapped executable code |
| **Development Loop** | Staged Git validation | `tests/test_mnerys_native_git_hooks.py`: native pre-commit hook checks staged blobs without running full shards |
| **Restart Recovery** | Stale turns marked interrupted with unknown effects | `tests/test_gateway_restart_recovery.py::test_restart_records_unknown_effects_once_without_replay` |
| **Restart Recovery** | Stale pending approvals retired as superseded | `tests/test_gateway_restart_recovery.py::test_restart_retires_pending_approval_without_restoring_authority` |
| **Restart Recovery** | Process boot cutoff protects live turns | `tests/test_gateway_restart_recovery.py::test_boot_cutoff_preserves_current_process_and_atomic_terminal_is_owner_bound` |
| **Restart Recovery** | Atomic rollback on storage failure | `tests/test_gateway_restart_recovery.py::test_terminal_event_storage_failure_rolls_back_status` |
| **Restart Recovery** | Expired / denied approval persistence | `tests/test_gateway_restart_recovery.py::test_restart_expires_old_approval_and_preserves_consumed_decision` |
| **Concurrency** | Exactly-once terminal settlement | `tests/test_gateway_restart_recovery.py::test_concurrent_terminal_writers_settle_exactly_once` |
| **Security** | Owner-scoped visibility for recovered turns | `tests/test_gateway_restart_recovery.py::test_recovered_status_and_events_are_visible_only_to_owner` |
| **Disconnect** | Exact Web run recovery & durable terminal | `tests/test_gateway_restart_recovery.py::test_web_disconnect_recovers_exact_run_and_durable_terminal` |
| **Authority** | History cards cannot restore executable authority | `tests/test_gateway_restart_recovery.py::test_web_pending_approval_is_metadata_only_and_cannot_replay` |
| **Authority** | Live process session binding only | `tests/test_api_token_tool_authority.py`, `tests/test_tool_approval_task_scope.py` |
| **Gateway v1** | Cancellation durability and finished-turn protection | `tests/test_mnerys_gateway_api.py::test_completed_turn_cannot_be_reported_as_cancelled`, `test_active_turn_cancellation_is_exact_and_durable` |
| **TUI** | TUI pump settles interrupted / decided approvals | `tests/test_mnerys_tui_chat.py`, `tests/test_mnerys_tui_approvals.py` |
| **Web Rendering** | UI renders interrupted status and unavailable cards | `tests/historyResumeRendering.test.mjs`, `tests/turnRendering.test.mjs`, `tests/frontendArchitecture.test.mjs` |
