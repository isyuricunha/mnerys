# M1 exit review — audited runtime and shared Gateway contract

**Milestone:** M1 — Audit and define the shared runtime contract

**Decision:** **Complete for architectural analysis and contract design** (2026-11-03).

**Not a claim of:** a production-ready Gateway, a second working transport adapter, an independent Agent Core, durable turns/events/approvals, a migrated session writer, or an installed CLI/TUI.

This review records the **M1 design milestone**, not the delivery acceptance of the future Gateway. The [roadmap](../ROADMAP.md) defines the product milestones. The [source audit](AGENT_CORE_GATEWAY_M1_AUDIT.md) and [proposed domain/API contract](GATEWAY_AGENT_CONTRACT.md) remain the technical evidence. The [M2 implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) owns the operational acceptance gates deferred from the original, overly broad M1 audit.

## Scope and exit decision

The approved M1 scope is **audit, design and bounded compatibility extraction**, not a complete production data migration. This resolves the earlier mismatch between the M1 title ("Audit and define") and a historical acceptance requirement to operate two real transports with restart-durable approvals. Those are **M2 implementation/acceptance obligations**, not silently satisfied tests.

| M1 design deliverable | Evidence | Status |
| --- | --- | --- |
| Inventory and reuse decisions for the inherited runtime, tools, MCP, skills, memory and scheduler | [Runtime audit](AGENT_CORE_GATEWAY_M1_AUDIT.md#findings--source-backed-inventory) | **Complete as source audit**; recheck dynamic paths during M2 |
| Document the Gateway, Core, persistence, tools and client authority boundary | [Shared contract](GATEWAY_AGENT_CONTRACT.md#goal-and-boundaries) | **Complete as design**; proposed APIs are not shipped |
| Define Principal, session, turn, event, approval, execution and idempotency semantics | [Domain objects](GATEWAY_AGENT_CONTRACT.md#shared-domain-objects--proposed-v1), [server interfaces](GATEWAY_AGENT_CONTRACT.md#server-interfaces-python-protocols-not-fixed-module-paths) | **Complete as contract proposal**, not implemented schema |
| Characterize current Web owner/scope policy, exact run cancellation, SSE replay and tool denials | [Regression baseline](M1_CONTRACT_TEST_BASELINE.md), [design-exit test](../../tests/test_mnerys_m1_design_exit.py) | **Validated for the inherited and internal paths** |
| Map session writers and durable migration prerequisites for SQLite and PostgreSQL 17 | [Writer inventory](SESSION_WRITER_INVENTORY.md), [revision plan](SESSION_REVISION_MIGRATION.md) | **Complete as initial inventory/plan**; live writer migration blocked |
| Preserve existing Web/session behavior and fail closed on security-boundary regressions | [CI and local evidence](M1_CONTRACT_TEST_BASELINE.md) | **Tested regression baseline**, not a live cross-client acceptance test |
| Identify M2 owners, order, stop conditions and acceptance criteria | [M2 implementation plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md) | **Documented** |

### Implemented and locally exercised during M1

The implementation remains a set of **internal seams around the inherited runtime**:

- [Trusted session admission](../../src/gateway_admission.py), [owner lookup](../../src/session_repository.py) and [server-derived execution limits](../../src/gateway_execution_policy.py) for existing server-admitted requests;
- [Gateway run facade](../../src/gateway_turns.py) and [internal TurnService](../../src/turn_service.py), delegating to the single existing [agent-run manager](../../src/agent_runs.py), **not** a parallel agent engine or an independent public Gateway;
- optional, **process-local** run-scoped SSE cursor/replay, without restart-safe event storage;
- [shared persisted-message preparation](../../src/session_message_preparation.py) and narrowly scoped inherited session/agent-tool cache fixes;
- isolated, **inactive** [incarnation/revision-fenced writer](../../src/session_write_fencing.py), background-delivery and cache-reconciliation prototypes, tested in disposable SQLite/PostgreSQL 17.

The [M1 exit test](../../tests/test_mnerys_m1_design_exit.py) verifies the **same internal policy facade** for browser and chat-scoped bearer **principal projections**, plus negative cases for foreign owner, missing scope, forged admission and delegated-tool restrictions. These are **test fixtures**, not independent Web/CLI/TUI adapters, and do not establish full policy parity between two deployed transports.

## Security and compatibility invariants preserved for M2

1. **Authentication and owner-scoped admission precede all turn/run operations.** A client-provided principal, claimed scope, session ID or cursor is not trusted proof of access.
2. **Tool authority remains server-owned.** Model proposals, slash commands, skills, MCP payloads, browser/TUI bridges and task resumes must never mint permissions. All execution follows the existing request-authority/policy/approval/dispatcher checks until an equivalent audited server boundary replaces them.
3. **An approval is an exact, owner/session/turn/resource-bound operation**, not general permission conveyed by a UI card. Durable approval consumption and restart behavior remain unimplemented.
4. **A session ID alone is not a lifetime fence.** The future live writer must check owner, incarnation and revision in the same transaction; all competing writers must participate.
5. **Web compatibility comes first.** Keep historical session/message data, existing settings, original authorization semantics and current Web endpoints until equivalent Gateway behavior is verified.
6. **SQLite remains the simple default; PostgreSQL 17 is a separate supported primary backend.** Auxiliary stores are tracked independently. Database URL changes do not move data.
7. **No production database migration or deployment is implied by M1 completion.** A reviewed backup, test restoration, migration compatibility plan and specific authorization precede activation.

## M2 delivery gates — explicitly *not* satisfied by M1

| Future gate | Current reality |
| --- | --- |
| Server-owned, credential-verifying public Gateway and stable versioned transport | **Planned**; existing Web routes and an internal facade are not the public Gateway |
| Two **real** transport adapters using one shared turn/permission contract | **Not tested**; only two credential projections have been simulated |
| Authenticated create, observe, cancel, resume and approval decision via one service | **Partial internal functionality**; approval and durable operations are not unified |
| Durable event ordering, cursor/gap semantics and restart recovery | **Planned**; current replay is process-local |
| Persisted, fenced, idempotent turns/messages, jobs and approvals | **Prototype only**; inherited writers remain unversioned |
| Backend migration, isolated backup/restore and all-writer concurrency parity | **Not done for the complete live schema** |
| Actual independent lightweight CLI/TUI clients and cross-client continuity | **Planned in later milestones** |
| Validated deployment/rollback of the new Gateway | **Not performed** |

These are binding constraints for the [M2 implementation and acceptance plan](M2_GATEWAY_IMPLEMENTATION_PLAN.md), not waived requirements. Treat M1 as **closed in design scope**, and M2 as **planned pending implementation**.

## Validation and release evidence

The [baseline](M1_CONTRACT_TEST_BASELINE.md) records historical local regressions, PostgreSQL isolation limits and corrected GitHub CI failures. During M1 design-exit validation, a fresh combined regression run passed **831 tests**, with **27 PostgreSQL-only skips** and one existing pytest-asyncio configuration warning. The new design-exit tests use only fake ownership lookup and an in-process run facade; they do not access production databases, secrets or network providers. These results are **local evidence**, not hosted CI for this documentation commit.

The [v0.2.0 GitHub Release](https://github.com/isyuricunha/mnerys/releases/tag/v0.2.0) has been observed, and the relevant [CI on commit a86e1cd4](https://github.com/isyuricunha/mnerys/actions/runs/41674047252) completed successfully. The [CI-gated v0.2.0 release workflow](https://github.com/isyuricunha/mnerys/actions/runs/41675068082) also completed successfully, including its ARM64 build, AMD64 build and manifest/version-tag merge jobs. Those workflow outcomes **do not prove** clean-machine installation, end-to-end container runtime behavior, CLI/TUI parity or native preview deployment. Follow the [release automation checks](../deployment/RELEASE_AUTOMATION.md) for each artifact.

## References and next step

- [Proposed contract and API/event semantics](GATEWAY_AGENT_CONTRACT.md)
- [M2 ordered work, test strategy, migration gates and stop conditions](M2_GATEWAY_IMPLEMENTATION_PLAN.md)
- [SQLAlchemy 2.0 transaction lifecycle](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [PostgreSQL 17 transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html)
- [Python Protocol definitions](https://docs.python.org/3/library/typing.html#typing.Protocol)

**Next engineering work belongs to M2:** implement one server-owned, authenticated turn/permission pathway and validate it with two genuine transports before exposing a public Gateway. Do not wire the revision-fenced writer into persistent data before the documented migration gates pass.
