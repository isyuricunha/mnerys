---
layout: default
---

# Mnerys tool approvals and YOLO

Mnerys uses one Gateway-owned permission system for the shared Agent Core.

## Normal mode

Read-only inspection is automatic when both the operation and its resources are
authorized: listing files, inspecting permitted source files, searching their
contents, and read-only Git or Docker queries.

Commands that can change state require approval, including creating/editing or
deleting files, arbitrary program execution, package installation, and service
changes. Unknown commands are not silently treated as read-only.

## YOLO mode

YOLO is an explicit, confirmed per-session setting. When enabled, it executes
**authorized** known operations without ordinary approval prompts, including
writing, editing and deleting files. The mode remains constrained by
authentication, filesystem isolation, operating-system privileges, server
tool policies, audit, and replay prevention.

## Important distinction

Approval-free does not mean unrestricted. Request authority and resource
confinement are checked independently for every tool invocation. A short
follow-up that loses its original intent is not fixed by broadening Bash
permissions; that continuity behavior is tracked separately.

See [M5.3 permission-mode architecture](https://github.com/isyuricunha/mnerys/blob/main/docs/architecture/M5_3_PERMISSION_MODES.md)
and the [current roadmap](https://github.com/isyuricunha/mnerys/blob/main/docs/ROADMAP.md).

This documentation describes the development contract. Published versions and
running self-hosted deployments may differ until the corresponding release and
deployment gates complete.
