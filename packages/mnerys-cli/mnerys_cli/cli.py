"""Mnerys command line adapter.

Entry point: ``mnerys``. Kept deliberately independent of the Mnerys web
server, database and agent loop: it is a lightweight client of the shared
authenticated Gateway API v1.
"""

import argparse
import sys

from mnerys_cli import __version__

DESCRIPTION = "Mnerys: authenticated turns against the shared Gateway."


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
    subparsers.required = False
    for name, help_text in (
        ("connect", "configure and select an authenticated Gateway profile"),
        ("chat", "run a non-interactive chat/agent turn"),
        ("agents", "list authorized agents"),
        ("models", "list available models"),
        ("tools", "list discoverable tools"),
        ("doctor", "diagnose installation, configuration and connectivity"),
        ("serve", "run a configured local server (local runtime required)"),
    ):
        subparsers.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in {
        "connect", "chat", "agents", "models", "tools", "doctor", "serve",
    }:
        print(
            f"mnerys: command '{args.command}' is implemented in this package.\n"
            "This standalone invocation is a placeholder; see --help.",
        )
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())