# M5.3 — Risk-First Approval Policy and Opt-in YOLO

## Scope

This contract changes **discretionary approval**, not the execution engine,
authenticated owner, tool catalog, resource authority, process sandbox or
filesystem confinement. One server-owned `ToolRunSecurityContext.decision_for`
serves the Web/CLI/TUI/Telegram agent loop and direct tool dispatcher.

The user-requested default is **automatic read-only inspection** and **ask
before state-changing or uncertain operations**. YOLO is an explicitly
confirmed, persistent per-session owner setting that bypasses ordinary
confirmation for authorized actions, including write/edit/remove and process
execution. Neither mode bypasses non-delegable denials.

## Normal mode

- A **concrete shell command** classified LOW_RISK by the deterministic
  `src/command_risk.py` can run without an ordinary prompt even when earlier
  results came from the Web or other untrusted tools. Empty/missing commands
  cannot inherit this exemption.
- Native tools with verified effects limited to `read_public`,
  `read_workspace`, `read_private`, `brokered_network_read` and
  `user_interaction` do not trigger a redundant approval card. A native
  tool's actual server-side authorization, owner and resource checks still
  execute. Calling `ask_user` may ask the user a clarification; it is not
  approval for executing another action.
- All other known effects require a normal approval, including file writes,
  edits, deletion, arbitrary code execution, network mutations and
  administrative changes. This applies even before an external Web result.
- Unknown/ambiguous effects or commands are not presumed safe.
  Sensitive paths, dynamic command substitution, redirections, script
  execution and composite commands are classified conservatively.

Commands like `git add`, `git commit`, `git pull`,
`docker compose up`, `touch`, `mkdir`, `ln`, `tee`,
`curl` and `wget` are **not** read-only. In particular, the older
classifier's broad "non-destructive git" and Docker fallbacks were corrected
to use actual read-only subcommand allowlists.

## YOLO mode

When enabled by the authenticated session owner and explicitly confirmed,
YOLO suppresses all *ordinary* discretionary prompts for known, admitted
operations, including `rm`, `Write`, `Edit`, shell and Python, whether
or not external context was encountered. A full prompt suppression is not a
blanket grant: unregistered tools, delegated credentials, DENIED operations,
cross-owner resources, tool-policy denials, sealed operation mismatches,
sensitive filesystem restrictions, read-before-edit, operating-system
privileges, audit and replay protection remain enforced.

YOLO cannot be enabled by model text, skills, MCP results or a prompt
injection. Its state and effective privilege must be explicitly visible
through the existing session controls and audit events.

## Two distinct gates

1. **Request authority and confinement:** Does this authenticated turn
   possess authority to use this tool and these resources?
2. **Risk-first discretionary approval:** Given an already admitted
   operation, can it execute automatically in the selected mode?

Allowing safe reads in the second gate does *not* turn a request containing
only `si` into a new grant for `ls`, nor grant general Bash authority
for a listing-only intent. That is an independent follow-up/intent-continuity
problem and is intentionally not hidden by expanding authorization here.

## Evidence and release state

Focused regressions:
`tests/test_risk_first_auto_approvals.py`,
`tests/test_command_risk_classifier.py`,
`tests/test_yolo_mode.py`, existing Gateway filesystem approvals,
tool-effect classification, and owner/approval tests.

Adopt current official permission-model concepts from
[Claude Code](https://code.claude.com/docs/en/permissions) and
[Kilo Code](https://kilo.ai/docs/getting-started/settings/auto-approving-actions)
for a separation of allow/ask/deny; these external tools do not define
Mnerys' server authority or override its documented security invariants.

A local commit and focused tests are not equivalent to hosted CI,
semantic-release, or deployment. Record each gate independently.
