"""Mnerys command line adapter.

Entry point: ``mnerys``. Kept deliberately independent of the Mnerys web
server, database and agent loop: it is a lightweight client of the shared
authenticated Gateway API v1.
"""

import argparse
import sys

from mnerys_cli import __version__
from mnerys_cli.errors import MnerysCliError

DESCRIPTION = "Mnerys: authenticated turns against the shared Gateway."


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
        "--version", action="version", version=f"mnerys {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

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
    connect.set_defaults(handler=lambda args: commands.cmd_connect(args))

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
    chat.set_defaults(handler=lambda args: commands.cmd_chat(args))

    agents = _common_parser(
        subparsers, "agents", "list authorized agents",
    )
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(handler=lambda args: commands._discover(args, "agents"))

    models = _common_parser(
        subparsers, "models", "list available models",
    )
    models.add_argument("--json", action="store_true")
    models.set_defaults(handler=lambda args: commands._discover(args, "models"))

    tools = _common_parser(
        subparsers, "tools", "list discoverable tools",
    )
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(handler=lambda args: commands._discover(args, "tools"))

    doctor = _common_parser(
        subparsers, "doctor", "diagnose installation, configuration, Gateway",
    )
    doctor.set_defaults(handler=lambda args: commands.cmd_doctor(args))

    serve = _common_parser(
        subparsers, "serve", "run a configured local server (local runtime required)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7021)
    serve.add_argument("--debug", action="store_true")
    serve.set_defaults(handler=lambda args: commands.cmd_serve(args))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
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