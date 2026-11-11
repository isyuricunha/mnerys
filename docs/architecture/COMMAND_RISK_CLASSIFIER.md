# M5.3 — Deterministic Command Risk Classifier (Increment 3)

`src/command_risk.py` is the one shared, Gateway-owned risk classification
boundary for shell/process execution. It is local, deterministic,
provider-independent and transport-independent; it runs inside the existing
approval gate (`ToolRunSecurityContext.decision_for`) immediately before the
four-decision approval flow and process launch — so every execution surface
(Web, CLI, TUI, MCP continuations, background jobs) is covered, not just one
tool spelling.

## Classification result

A structured result: category, level, human-readable reason, matched segment,
resource scope, analysis-completeness status, requires-approval flag, and a
stable classifier version (`COMMAND_RISK_VERSION = 1`).

**Categories and precedence**

| Category | Meaning | Mapping |
| --- | --- | --- |
| `LOW_RISK` | confidently understood, non-destructive | no additional risk-triggered prompt; ALL other gates still apply |
| `REQUIRES_APPROVAL` | potentially destructive, sensitive, privileged, or insufficiently understood | flows into the existing four-decision approval flow with the concrete risk reason |
| `DENIED` | non-delegable prohibition | refused outright; no approval can lift it |

Classification NEVER grants authority: LOW_RISK only means no additional
risk-based approval is necessary. Admission, tool policy, request-authority
grants, resource/CWD bindings, the external-untrusted-context gate,
read-before-edit and derived-artifact publication remain intact for every
execution.

## Parsing strategy

* Quote-aware top-level splitting over `&&`, `||`, `;`, newlines and `&`;
  pipelines (`|`) are classified as one data-flow expression; each pipeline
  stage is also classified individually.
* `shlex` (POSIX mode) performs LEXICAL analysis for argv extraction — never
  treated as a shell interpreter. Lexing failure or unbalanced quoting fails
  closed (REQUIRES_APPROVAL, analysis incomplete).
* Quoted literal text is not an executable operation: single-quoted text is
  stripped entirely; double-quoted text remains visible to the dynamic-content
  detection (the shell expands substitutions and variables inside it).
* Wrappers recurse: `sh -c`, `bash -c` (the lexed payload), `env`, `command`,
  `nohup`, `timeout`, `nice`, `stdbuf`, `setsid`; `sudo`/`doas`/`pkexec`
  escalate to high risk; `xargs` requires approval (argument delegation).
* Conservative fallbacks: unknown executables, command/process substitution
  (`$()`, backticks, `<(`), heredoc bodies, script-file invocation, inline
  interpreter code (`python -c`, `node -e`), and any construct that cannot be
  analyzed reliably classify as REQUIRES_APPROVAL — never guessed safe.
* Nothing is executed to determine its risk.

## Risk taxonomy (extensible, operation-grounded)

Filesystem destruction (recursive/root/wildcard deletion, file wipes),
storage/system administration (formatting, raw device writes, mounts,
shutdown, critical service management), privilege escalation, repository
operations (destructive resets/cleans, forced pushes, history rewrites —
with safe inspection differentiated), infrastructure (Docker/Compose
destruction with read-only inspection differentiated), database destruction,
remote execution and dynamic code (download-and-execute via pipeline or
substitution, uninspected scripts, interpreters), and sensitive resources
(`.ssh`, `.env`, credentials — reading OR mutating; credential
access/exfiltration is itself the risk).

The taxonomy is a RISK layer, not a denial list: any operation may legitimately
run after proper authorization through the four-decision flow.

## Risk-to-approval mapping

A REQUIRES_APPROVAL classification refuses the gate with the risk reason; the
agent loop's existing branch then either matches an exact scoped rule
(suppressing the repeat prompt for that exact operation) or creates a genuine
pending approval presenting the operation, the risk reason, the effective CWD
and the four decisions. The immediate action resumes only through its sealed
original operation with one-use execution, denial, expiry, cancellation and
replay protections. The risk gate is independent of the task/session scope
bypasses: a dangerous operation within a task-approved run still prompts.
Unattended tools explicitly granted by a trusted local surface keep their
grant (documented); delegated credentials keep their non-delegable boundary.

## Saved-rule interactions

The classifier adds no new rule semantics: the increment-2 exact-operation
rules keep their narrow matching. The operation's capability fingerprint
already fails closed when classification changes; risk classification does
not introduce prefix/wildcard approvals.

## Known limitations (honest boundaries)

* A Bash rule cannot see inside opaque blobs: encoded/obfuscated commands,
  base64 pipes, and dynamically CONSTRUCTED commands (variables assembling an
  executable name) classify conservatively but the true effect is only
  enforced by the sandbox and OS permissions, not by classification.
* Wrappers not in the analyzed set (a renamed copy of a shell) classify as
  unknown — approval is required, which is the safe behavior.
* Command classification is an ADDITIONAL decision layer; it does not make
  unrestricted shell execution universally safe, and it never replaces the
  filesystem sandbox, OS permissions, or Gateway authorization.
* MCP tools execute on external servers with their own isolation; they are
  not assumed to pass through the local shell boundary.

## Test evidence (increment 3)

`tests/test_command_risk_classifier.py` — 68 deterministic tests: 14 low-risk
recognitions, quoted/comment false-positive checks, ~40 dangerous-operation
detections with exact levels (recursive/root deletion, destructive git,
Docker/Compose, storage/system, privilege, databases, sensitive paths,
download-and-execute pipelines and substitutions, nested shells, inline
interpreters, compound commands, find -exec/-delete, awk system()), the
conservative fallbacks (unknown executables, unbalanced quoting, failed
lexing), background markers, python-code AST classification, and the Gateway
integration (risk prompts, scoped-rule suppression for the exact reviewed
operation only, policy-denial independence, delegated boundaries, stale
fingerprint refusal).
