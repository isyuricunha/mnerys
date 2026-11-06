"""Mnerys command line adapter.

Entry point: ``mnerys``. Kept deliberately independent of the Mnerys web
server, database and agent loop: it is a lightweight client of the shared
authenticated Gateway API v1.
"""

import argparse
import sys

from mnerys_cli.config import load_profiles, resolve_endpoint
from mnerys_cli.errors import MnerysCliError
from mnerys_cli.version import distribution_version

DESCRIPTION = "Mnerys: authenticated turns against the shared Gateway."


class _VersionAction(argparse.Action):
    """Print the installed distribution version when the flag is used.

    Resolution happens only on ``--version`` so a broken or missing
    installation still allows ``--help`` and every other command, while the
    version flag itself fails loudly (exit 6) instead of reporting a wrong
    release.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            version = distribution_version()
        except MnerysCliError as exc:
            print(f"mnerys: {exc}", file=sys.stderr)
            raise SystemExit(exc.exit_code) from None
        print(f"mnerys {version}")
        raise SystemExit(0)


def _common_parser(subparsers, name: str, help_text: str) -> argparse.ArgumentParser:
    """A parser with the shared --profile selector, without importing the
    network client (so trivial help/version invocations stay dependency-free)."""
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--profile", default=None, help="named profile to use")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnerys",
        description=DESCRIPTION,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version", action=_VersionAction, nargs=0,
        help="print the installed mnerys-cli version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    tui = subparsers.add_parser(
        "tui", help="launch the interactive Mnerys terminal interface",
    )
    tui.add_argument("--profile", default=None, help="named profile to use")

    connect = subparsers.add_parser(
        "connect", help="configure and select an authenticated Gateway profile",
    )
    connect.add_argument("--profile", default="default", help="profile name")
    connect.add_argument("--endpoint", default=None, help="Gateway base URL")
    connect.add_argument("--token", default=None, help=argparse.SUPPRESS)
    connect.add_argument(
        "--token-stdin", action="store_true",
        help="read the API token from stdin (avoid shell history)",
    )

    chat = subparsers.add_parser(
        "chat", help="run a non-interactive chat/agent turn",
    )
    chat.add_argument("message", nargs="?", default=None)
    chat.add_argument("--profile", default=None)
    chat.add_argument("--session", default=None, help="target session id")
    chat.add_argument(
        "--mode", choices=("chat", "agent"), default="chat",
        help="turn composition mode",
    )
    chat.add_argument("--idempotency-key", default=None)
    chat.add_argument("--json", action="store_true", help="structured output")

    agents = _common_parser(
        subparsers, "agents", "list authorized agents",
    )
    agents.add_argument("action", nargs="?", choices=("list",),
                        help=argparse.SUPPRESS)
    agents.add_argument("--json", action="store_true")

    models = _common_parser(
        subparsers, "models", "list available models",
    )
    models.add_argument("action", nargs="?", choices=("list",),
                        help=argparse.SUPPRESS)
    models.add_argument("--json", action="store_true")

    tools = _common_parser(
        subparsers, "tools", "list discoverable tools",
    )
    tools.add_argument("action", nargs="?", choices=("list",),
                       help=argparse.SUPPRESS)
    tools.add_argument("--json", action="store_true")

    _doctor = _common_parser(
        subparsers, "doctor", "diagnose installation, configuration, Gateway",
    )

    serve = _common_parser(
        subparsers, "serve", "run a configured local server (local runtime required)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7021)
    serve.add_argument("--debug", action="store_true")

    return parser


def _interactive_terminal() -> bool:
    """True when both stdin and stdout are attached to a TTY."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _launch_tui(args: argparse.Namespace) -> int:
    """Resolve the profile and hand the loop to the Textual application."""
    profiles = load_profiles()
    profile = getattr(args, "profile", None)
    try:
        endpoint, token = resolve_endpoint(profiles, profile)
    except MnerysCliError as exc:
        print(f"mnerys: {exc}", file=sys.stderr)
        return exc.exit_code
    from mnerys_cli.tui import run_tui

    try:
        return run_tui(endpoint, token, profile=profile)
    except MnerysCliError as exc:
        print(f"mnerys: {exc}", file=sys.stderr)
        return exc.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if _interactive_terminal():
            # An interactive terminal attached to a real profile launches the
            # TUI; everything else keeps the classic help behavior.
            return _launch_tui(args)
        parser.print_help()
        return 0
    if args.command == "tui":
        return _launch_tui(args)
    # Commands import the network client lazily so --version/--help work
    # even when optional runtime dependencies are missing or installing.
    from mnerys_cli import commands

    def run() -> int:
        if args.command == "connect":
            return commands.cmd_connect(args)
        if args.command == "chat":
            return commands.cmd_chat(args)
        if args.command == "agents":
            return commands._discover(args, "agents")
        if args.command == "models":
            return commands._discover(args, "models")
        if args.command == "tools":
            return commands._discover(args, "tools")
        if args.command == "doctor":
            return commands.cmd_doctor(args)
        if args.command == "serve":
            return commands.cmd_serve(args)
        parser.print_help()
        return 2

    try:
        return int(run() or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 5
    except MnerysCliError as exc:
        print(f"mnerys: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    sys.exit(main())