# Mnerys release automation

Status: **configured, not published**. A committed workflow is not evidence of an actual release.

## CI and versioning

- GitHub is the primary release registry. Only a successful same-repository push to main is eligible.
- .github/workflows/release.yml runs after the existing full CI workflow passes. It checks that the tested SHA is still main HEAD before publishing.
- .releaserc.json pins semantic-release 25.0.9 and Conventional Commits. The npm package is private: nothing is published to npm.
- GitHub's short-lived GITHUB_TOKEN is used for the GitHub Release. No personal access tokens or private endpoints are embedded.

## First release: v0.1.0

Semantic-release does not natively initialize projects at 0.x; see its official FAQ. A one-time guarded bootstrap creates the initial v0.1.0 GitHub Release with [reviewed notes](../releases/0.1.0.md). The bootstrap is only eligible after CI passes. Subsequent versions are computed automatically from Conventional Commits.

| Commit | Effect during 0.x development |
| --- | --- |
| feat | Minor: 0.1.0 to 0.2.0 |
| fix, perf, refactor, security, revert | Patch: 0.1.0 to 0.1.1 |
| BREAKING CHANGE or feat! | Minor in 0.x; promotion to 1.0.0 requires an explicit policy review |
| docs, ci, test, build, chore, style alone | No new release |

The release-notes generator groups Features, Bug Fixes, Performance, Internal Improvements, Security, Reverts, Documentation, Build System, CI/CD, Tests and Maintenance as applicable.

## GHCR container images

The existing multiarchitecture Docker workflow now supports reusable release builds. GitHub does not start a new push workflow from tags created using GITHUB_TOKEN, so the release job invokes the image workflow directly.

For v0.1.0, expected tags are v0.1.0 (release), v0 (rolling major line), latest (current stable) and v0.1.0-<sha> (traceable build). Later lines use v1 and v2. Dev images remain separate: dev and dev-<sha>.

Release images inject MNERYS_RELEASE_VERSION from the checked Git tag. This controls the version shown by the runtime API. A native checkout uses a development baseline plus a separate source-commit identifier; this does not prove a release was published.

No globally installable Mnerys CLI is published yet. The CLI, TUI and independent Gateway remain planned.

## Validation and failures

- Current CI must be green; any red or cancelled CI run blocks GitHub Release publication.
- The new release rules, workflow permissions, CI gate, tag derivation and build-version behavior have dedicated tests.
- Publishing the GitHub Release and publishing container images are separate jobs. If the latter fails, investigate and rerun that job; do not invent another version to disguise a failure.
- Version tagging and GHCR publication do not redeploy the private native preview or modify its PostgreSQL database.

## CI regression baselines during rebranding

The complete Python CI remains blocking. Rebranding must update outdated assertions rather than restore upstream branding or disable tests:

- README checks require the **Mnerys** wordmark and explicit `AUTH_ENABLED=true` / `LOCALHOST_BYPASS=false` deployment guidance.
- The generated website configuration reference must be regenerated and verified with `python3 scripts/generate_env_reference.py --check` when source reads change.
- The PWA manifest and icon tests validate the current Mnerys assets. The archived Odysseus restoration hashes remain untouched; the current three rebranded icons are intentionally not byte-identical to the historical assets. All other restored artifacts still require their recorded hashes.
- The computed-style snapshot baseline is measured against the approved Eclipse default. Baseline regeneration requires an actual Playwright capture and explicit design review, not a blanket removal of the regression gate.
- The gallery editor's offline cache test follows the current `mnerys-v*` service-worker cache naming scheme.
- `scripts/build/README.md` is tracked as source documentation despite the generic `build/` output exclusion; CI must not depend on an ignored local-only file.
- GitHub Pages guides use published-site-relative links only for files inside `website/`. Documentation outside that source uses public GitHub repository links instead.

Local focused tests do not replace a successful GitHub Actions run on the published commit; the first release remains blocked until the full hosted CI succeeds.

## GitLab and Forgejo mirrors

Automated tag synchronization is **not configured**: the GitHub repository has no mirror-publishing credentials. After an explicitly authorized publication, synchronize and verify release tags with the existing secure Git remotes; do not add private addresses, tokens or infrastructure data to this public workflow.

## Rollout

1. Validate the workflow and all affected tests locally; review the public diff for leaks.
2. Publish to GitHub and the configured source mirrors only under the project's authorization policy.
3. Verify a successful full CI run on the exact main SHA; this releases v0.1.0 at most once.
4. Verify the release notes/tag SHA, arm64/amd64 container manifests and runtime version.
5. Separately synchronize published release tags to the other remotes using an authorized private workflow.

Official guidance: https://semantic-release.gitbook.io/semantic-release/support/faq
