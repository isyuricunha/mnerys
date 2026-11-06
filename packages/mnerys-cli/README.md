# mnerys-cli

The command line client for [Mnerys](https://github.com/isyuricunha/mnerys).
`mnerys` is a lightweight adapter of the shared authenticated Gateway API:
it never imports the web server, the database stack or the agent loop, and
it never decides approvals or session ownership on its own.

## Install

```bash
uv tool install /path/to/packages/mnerys-cli
# or, from a built wheel:
uv tool install --from mnerys-cli mnerys
```

Installation is per-user; no `sudo` or symlink hacks are required.

## Commands

- `mnerys --version` — installed CLI version.
- `mnerys --help` — complete command documentation.
- `mnerys connect` — configure and select an authenticated Gateway profile.
- `mnerys chat "message"` — non-interactive chat/agent turn.
- `mnerys agents list`, `mnerys models list`, `mnerys tools list` — discovery.
- `mnerys doctor` — diagnose installation, configuration and connectivity.
- `mnerys serve` — run a configured local server (local runtime required).

Secrets are stored with restrictive permissions and never echoed.