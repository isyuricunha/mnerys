# Skills lifecycle

The UI exposes All, Built-in, Approved, and Draft. Draft includes archived
records so they remain inspectable and recoverable. Built-ins are not audited.
Approved means published, passing, at the configured confidence threshold,
and not marked unnecessary. Baseline speed measurements remain evidence, not
an additional hidden UI approval gate.

Automatic audits process at most eight eligible records at a time, oldest first.
New records are eligible immediately; inconclusive checks retry after a day;
failed repairs retry after a week. Passed, duplicate-skipped, and archived records
are excluded. Existing daily Skills Audit tasks drive this queue. Their quiet
window deferrals propagate to the scheduler rather than becoming task failures.
Automatic runs use background model scheduling. Existing self-repair and teacher
repair stages remain in place; failed candidates remain drafts.

The skill index advertises short descriptions; the agent loads a relevant full
procedure on demand and applies already-injected procedures directly. Extraction
prefers verified discoveries and specific workarounds over routine tool usage.

## M5.2 increment 5 — canonical `Skill` tool

The model-facing canonical `Skill` tool (handler `activate_skill`) activates
installed skills through this same library and its gates:

- With no arguments it returns the compact eligible index (name/description/
  category, capped) — the same list the system prompt carries; bodies stay
  lazy and are never injected per request.
- `Skill(name)` loads one skill's SKILL.md (bounded) with provenance;
  `Skill(name, path)` reads one reference file confined to the skill's
  directory (symlinks and traversal refused; size-bounded).
- Eligibility uses the shared `activation_eligible` gate in
  `services/memory/skills.py` — the single enforcement point the loop's
  relevance injection and this tool both apply (built-ins always; otherwise
  published + passing audit + owner confidence threshold).
- Skill content is untrusted instructions: activation grants no permission,
  changes no tool policy, disables no approval and executes nothing.

Reference reviewed: NousResearch/hermes-agent, MIT license, commit
cfdbbb6e35010ace89fbe8243ee82fa4de143e10, cloned to
<configured-path> In particular tools/skills_tool.py and
agent/prompt_builder.py use progressive disclosure and task-triggered procedure
loading. These changes adapt that approach to Odysseus's existing registry;
no Hermes implementation code was copied.
