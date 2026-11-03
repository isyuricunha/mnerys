# Mnerys Documentation

This index is the entry point for **current Mnerys documentation**. Documents inherited from Odysseus are retained for attribution and technical history, but are not automatically Mnerys policies or descriptions of completed functionality.

## Product and architecture

- [Mnerys roadmap](ROADMAP.md) — official priorities and planned milestones, including the global CLI, TUI, Agent Core and Gateway.
- [Architecture index](architecture/README.md) — M1 Agent Core/Gateway source audit, internal seams and inherited contracts.
- [M1 design exit review](architecture/M1_EXIT_REVIEW.md) — verified audit/contract-design completion, not a shipped independent Gateway.
- [M2 Gateway execution plan](architecture/M2_GATEWAY_IMPLEMENTATION_PLAN.md) — ordered implementation, authorization, migration and cross-transport acceptance gates.
- [Frontend architecture](frontend-architecture/handbook.md) — frontend design and implementation notes; verify historical assessments against current code.
- [Runtime decomposition](runtime-decomposition/COMPARISON_PROTOCOL.md) — imported analysis and tests from the runtime refactoring effort; not a substitute for a new Mnerys architectural contract.

## Operations and security

- [Native preview deployment](deployment/NATIVE_PREVIEW.md) — native systemd services, validation and rollback.
- [Release automation](deployment/RELEASE_AUTOMATION.md) — CI-gated semantic-release, initial 0.1.0, notes and GHCR version tags.
- [Release notes](releases/README.md) — reviewed initial notes and publication status.
- [PostgreSQL 17 support](deployment/POSTGRESQL17.md) — optional primary database, compatibility and migration boundaries. SQLite remains the product default.
- [Historical volume-migration guide](deployment/migrations/README.md) — guidance only for applicable older ChromaDB deployments.
- [Security documentation](security/README.md) — inherited threat model and transport/trust notes; verify facts before applying.
- [Maintainer handbook](maintainers/README.md) — contributor and review guidance, some inherited from Odysseus.

## Identity and evidence

- [Mnerys visual identity](branding/README.md) — default Eclipse interface theme, retained presets and logo.
- [Nery Mizuki mascot](mascot/README.md) — artwork, approved character identity and separate license.
- [Screenshot provenance](screenshots/README.md) — real captures, dates and limitations.

## Historical records

- [Odysseus archive](archive/odysseus/README.md) — original roadmap, publication evidence, QA material, audits and reviews.
- Keep existing upstream attribution, notices and licenses intact. The root-level **LICENSE**, **ACKNOWLEDGMENTS.md**, **THIRD_PARTY_PROVENANCE.json**, **SECURITY.md** and **CONTRIBUTING.md** remain at their conventional/distribution paths intentionally.

## Directory rules

- The repository root holds **entry points and build/install contracts** (app, package metadata, dependency manifests, Compose, required license and project files).
- Active documentation belongs in subject directories under `docs/`. Use `docs/archive/odysseus/` for inherited records that should not define current Mnerys policy.
- Legacy build recipes belong in `scripts/build/`; the legacy service installer belongs in `scripts/legacy/odysseus/`. The user-facing startup launchers stay at the root for compatibility.
- Do not put private addresses, network topology, secrets, real messages or operational configuration into version-controlled documentation.
- When moving a document, update real links/tests; historical measurements and attribution must not be silently rewritten as if they were new findings.
