# mnerys CLI

The global `mnerys` command is a lightweight adapter of the shared
authenticated [Gateway API v1](architecture/GATEWAY_AGENT_CONTRACT.md). It
never imports the web server, the database or the agent loop, and it never
decides approvals or session ownership: those remain server-side Gateway
authority.

## Installation

The CLI lives in `packages/mnerys-cli` and installs per user:

```bash
uv tool install /path/to/mnerys/packages/mnerys-cli
mnerys --version
```

A regular `pip install .` inside the package directory works as well. The
only runtime dependency is `httpx`; a remote-only install does not install
the server, the database stack or the agent runtime.

Every Mnerys release also publishes a version-matched wheel and sdist as
GitHub Release assets, so the CLI installs without Git or a repository
checkout:

```bash
uv tool install https://github.com/isyuricunha/mnerys/releases/download/vX.Y.Z/mnerys_cli-X.Y.Z-py3-none-any.whl
```

Upgrade: run the same install command again after a new release; upgrades
preserve profiles, stored tokens and server data.
Uninstall:

```bash
uv tool uninstall mnerys-cli
```

Uninstalling the CLI never touches server data or the configuration of an
installed Mnerys server.

## Commands

```text
mnerys --version
mnerys --help
mnerys connect --endpoint https://gateway.example.com
mnerys chat "message" [--session ID] [--mode chat|agent] [--json]
mnerys agents list
mnerys models list
mnerys tools list
mnerys doctor
mnerys serve [--host 127.0.0.1] [--port 7021]
mnerys tui [--profile NAME]
```

## Interactive TUI

`mnerys tui` launches the first-party terminal interface (Textual), and
bare `mnerys` in an interactive terminal launches the same interface; in a
pipe or script, bare `mnerys` keeps printing the command help instead of a
full-screen application. The TUI reuses the named profiles and stored
tokens exactly like every other command (`--profile NAME` selects one) and
reports the unified release version.

The TUI extra is optional and keeps remote-only installs light:

```bash
uv tool install --force --with textual <mnerys_cli wheel URL>
# or, on a source install: pip install 'mnerys-cli[tui]'
```

A base installation without the extra answers `mnerys tui` with the
installation hint above and exit code 3. The shell shows the real Gateway
connection state (endpoint, version, discovery counts); the conversation
screen loads the session's persisted history and streams turns, with `q`
quit, `?` help, `r` refresh, `ctrl+s` sessions, `enter` send,
`alt+enter` newline and `escape` cancelling the active turn. Tool activity
renders as human-readable status lines and pending approvals show a card
whose `a` (approve) and `d` (deny) keys submit the decision through the
sealed server store; the displayed outcome is always the server's.

## Profiles and credentials

`mnerys connect --profile work --endpoint ...` stores a named profile in
the platform user configuration directory:

- Linux: `$XDG_CONFIG_HOME/mnerys` (default `~/.config/mnerys`)
- macOS: `~/Library/Application Support/mnerys`
- Windows: `%APPDATA%\mnerys`

The connector prompts for the API token (or reads it with
`--token-stdin`); the token is stored in `credentials/<profile>.token`
with restrictive permissions and never appears in profiles JSON, command
history, logs or output. `MNERYS_ENDPOINT`, `MNERYS_TOKEN` and
`MNERYS_PROFILE` override profiles for automation; `--profile` selects a
named profile. Tokens are never transmitted on the command line when the
interactive prompt or stdin path is used.

## Remote mode

Remote mode points `chat`, `agents`, `models`, `tools` and `doctor` at an
existing Mnerys Gateway. `chat` streams sanitized `message.delta` frames
until the deterministic terminal event, prints approval-required and
failure states without leaking tool payloads, and supports `Ctrl+C`
cancellation through the exact-turn cancel contract (exit code 5). JSON
output is available with `--json`.

## Local mode

When a local Mnerys runtime is installed, `mnerys serve` starts the same
application the preview uses (uvicorn `app:app`) under the directories
declared by the operator:

```bash
export MNERYS_APP_DIR=/path/to/mnerys-source-or-release
export MNERYS_APP_PYTHON=/path/to/runtime/venv/bin/python   # optional
mnerys serve --port 7021
```

`mnerys chat` then talks to that local server through the identical
Gateway contract; no separate agent engine exists in the CLI. Remote-only
installations get an explicit error from `serve` instead of a partial
runtime.

## Version policy

Mnerys uses one unified release version. The Git tags that semantic-release
creates (`vX.Y.Z`) are the only authority for released versions; the
`mnerys-cli` distribution derives its version from the corresponding tag at
build time and keeps no independent version sequence of its own.

- `mnerys --version` reports the version of the installed distribution
  metadata and never reads Git at runtime.
- A release tag build is exact: tag `vX.Y.Z` produces package version
  `X.Y.Z`, and the wheel metadata, sdist metadata and installed CLI all
  agree, verified by `scripts/verify_cli_release.py` in the release
  workflow.
- Builds from a development tree are identifiable development builds
  (`X.Y.Z.devN+g<hash>`), never a released version number.
- A missing or invalid installation fails with exit code 6 instead of
  reporting a wrong version.
- The wheel and sdist are attached to the corresponding GitHub Release;
  there is no PyPI package for the CLI.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | runtime/network/server error |
| 2 | usage error |
| 3 | configuration error (no profile, no local runtime) |
| 4 | authentication/authorization failure (401/403) |
| 5 | cancelled by the user |
| 6 | version metadata unavailable (broken installation) |

## Security notes

- Delegated credentials never decide approvals: an approval-required turn
  prints the pending state and the authorized approval path (for example
  the Web UI); the CLI does not simulate a browser approval.
- The CLI validates the endpoint scheme (`http://` or `https://`) and
  never disables TLS certificate verification.
- Server responses are data: tool output is rendered as text, never
  executed or interpreted as instructions.
- Private deployment details (Tailscale addresses, ports, tokens) are
  never part of CLI examples or fixtures.

## Cross-platform status

Linux is covered by the packaging, command and live acceptance tests on
the CI runners, including UTF-8 output and signal handling. The
configuration directory, executable naming and signal paths have
explicit Windows and macOS branches, but those platforms are not yet
verified on dedicated runners; treat them as implemented-but-unverified
until acceptance evidence exists.