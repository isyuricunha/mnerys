"""The interactive Mnerys terminal interface.

The TUI is one more Gateway client adapter: it owns no session store, no
policy and no approvals. Everything it shows comes from an authenticated
Gateway v1 response. Textual is an optional dependency extra
(``mnerys-cli[tui]``); a base installation prints an explicit installation
hint instead of a traceback.
"""

import asyncio
from importlib.util import find_spec

from mnerys_cli.errors import ConfigError

TEXTUAL_EXTRA_HINT = (
    "the Mnerys TUI needs the optional terminal extra; "
    "install it with: pip install 'mnerys-cli[tui]' "
    "(or: uv tool install --force --with textual <wheel-url>)"
)


def _require_textual():
    """Fail with an actionable message when the TUI extra is missing."""
    if find_spec("textual") is None:
        raise ConfigError(TEXTUAL_EXTRA_HINT)


def run_tui(endpoint: str, token: str | None, *, profile: str | None = None) -> int:
    """Launch the interactive TUI against one Gateway endpoint.

    Blocking call: it runs the Textual application until the user exits and
    returns the process exit code. The Gateway client lives inside the same
    asyncio loop as the application and closes before the loop does.
    """

    async def _run() -> int:
        from mnerys_cli.async_client import AsyncGatewayClient

        client = AsyncGatewayClient(endpoint, token)
        try:
            from mnerys_cli.tui.app import MnerysTui

            app = MnerysTui(client, endpoint, profile=profile)
            await app.run_async()
        finally:
            await client.close()
        return 0

    _require_textual()
    return asyncio.run(_run())
