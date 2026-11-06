"""The mnerys command implementations.

Every command is a thin adapter over the shared Gateway API. No command
creates its own agent engine, policy or approval authority.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys

from mnerys_cli import __version__
from mnerys_cli.client import GatewayClient
from mnerys_cli.config import (
    ConfigError as ProfileConfigError,
    load_profiles,
    resolve_endpoint,
    save_profile,
)
from mnerys_cli.errors import (
    AuthError, Cancelled, ConfigError, MnerysCliError, UsageError,
)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

def cmd_connect(args: argparse.Namespace) -> int:
    endpoint = args.endpoint or os.environ.get("MNERYS_ENDPOINT")
    if not endpoint:
        raise UsageError("connect requires --endpoint (or MNERYS_ENDPOINT)")
    token: str | None = os.environ.get("MNERYS_TOKEN")
    if not token:
        if args.token_stdin:
            token = sys.stdin.readline().strip() or None
        elif args.token:
            token = args.token
        elif sys.stdin.isatty():
            try:
                token = getpass.getpass("Gateway token (leave empty for none): ")
            except (EOFError, KeyboardInterrupt):
                raise Cancelled("interrupted") from None
        else:
            token = None
    save_profile(args.profile, endpoint, token)
    print(f"Profile '{args.profile}' configured for {endpoint.rstrip('/')}")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    print(f"mnerys cli version: {__version__}")
    print(f"python: {sys.version.split()[0]} on {sys.platform}")
    try:
        profiles = load_profiles()
    except ProfileConfigError as exc:
        problems.append(str(exc))
        profiles = {"active": None, "profiles": {}}
    profile = profiles.get("active")
    print(f"active profile: {profile or '(none)'}")
    if not profile and not os.environ.get("MNERYS_ENDPOINT"):
        problems.append("no profile configured; run 'mnerys connect'")
    client = None
    try:
        endpoint, token = resolve_endpoint(profiles, args.profile)
        print(f"endpoint: {endpoint}")
        client = GatewayClient(endpoint, token)
        status, body = client.health()
        print(f"health: HTTP {status}")
        if status != 200:
            problems.append(f"endpoint health returned HTTP {status}")
        else:
            try:
                models = client.models()
                print(f"gateway auth: OK ({len(models)} models visible)")
            except AuthError as exc:
                problems.append(str(exc))
            except MnerysCliError as exc:
                problems.append(str(exc))
    except (ProfileConfigError, ConfigError, MnerysCliError) as exc:
        problems.append(str(exc))
    finally:
        if client is not None:
            client.close()
    if problems:
        print("problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 3 if problems[0].startswith("no profile") else 1
    print("doctor: OK")
    return 0


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def _discover(args: argparse.Namespace, kind: str) -> int:
    _require_profile(args)
    with _client_for(args) as client:
        items = getattr(client, f"{kind}")()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    for item in items:
        if kind == "tools":
            print(item)
        else:
            name = item.get("id") or item.get("name") or ""
            extra = ""
            if kind == "models" and item.get("host"):
                extra = f"  ({item['host']})"
            print(f"{name}{extra}")
    return 0


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

def cmd_chat(args: argparse.Namespace) -> int:
    if not args.message:
        raise UsageError("chat requires a message (quote it) or --json input")
    profiles = load_profiles()
    endpoint, token = resolve_endpoint(profiles, args.profile)
    client = GatewayClient(endpoint, token)

    def _cancel(_signum, _frame):  # POSIX signal path
        raise KeyboardInterrupt

    if hasattr(_cancel, "__name__") and sys.platform != "win32":
        try:
            import signal
            signal.signal(signal.SIGINT, _cancel)
        except (ValueError, OSError):
            pass

    turn_id: str | None = None
    session_id: str | None = None
    try:
        session_id = args.session or _pick_or_create_session(client, endpoint)
        created = client.create_turn(
            session_id, args.message, mode=args.mode,
            idempotency_key=args.idempotency_key,
        )
        turn_id = created["turn_id"]
        _stream_turn(client, turn_id, session_id, args)
        return 0
    except KeyboardInterrupt:
        if turn_id and session_id:
            try:
                client.cancel_turn(turn_id, session_id)
                print("\n[cancelled]", file=sys.stderr)
            except MnerysCliError:
                print("\n[cancel request failed]", file=sys.stderr)
        raise Cancelled("interrupted") from None
    finally:
        client.close()


def _require_profile(args: argparse.Namespace) -> None:
    load_profiles()


def _client_for(args: argparse.Namespace) -> GatewayClient:
    profiles = load_profiles()
    endpoint, token = resolve_endpoint(profiles, args.profile)
    return GatewayClient(endpoint, token)


def _pick_or_create_session(client: GatewayClient, endpoint: str) -> str:
    """Reuse the most recent existing session or create a fresh one."""
    if os.environ.get("MNERYS_SESSION_ID"):
        return os.environ["MNERYS_SESSION_ID"]
    sessions = client.sessions()
    if sessions:
        return sessions[0]["id"]
    name = os.environ.get("MNERYS_TEST_SESSION") or f"cli-{os.getpid()}"
    response = client._request(
        "POST", "/api/v1/sessions",
        json_body={"name": name},
    )
    data = response.json()
    return data.get("id") or data.get("session_id")


def _stream_turn(
    client: GatewayClient, turn_id: str, session_id: str,
    args: argparse.Namespace,
) -> None:
    after = 0
    rendered_any = False
    for _round in range(1200):
        items = client.turn_events(turn_id, after_seq=after)
        for item in items:
            sequence = int(item.get("sequence", 0))
            kind = item.get("kind", "")
            payload = item.get("payload") or {}
            if kind == "message.delta":
                text = payload.get("text", "")
                if text:
                    print(text, end="", flush=True)
                    rendered_any = True
            elif kind == "message.final":
                text = payload.get("text", "")
                if text and not rendered_any:
                    print(text, end="", flush=True)
            elif kind == "turn.completed":
                after = sequence
                if args.json:
                    summary = _summary(client, turn_id)
                    json_summary = {"turn_id": turn_id, **summary}
                    if rendered_any:
                        print()
                    print(json.dumps(json_summary, ensure_ascii=False, indent=2))
                else:
                    print() if rendered_any else None
                return
            elif kind == "turn.awaiting_approval":
                print(
                    "\n[approval required: this turn waits for an owner-approved "
                    "decision in the Web or another authorized path]",
                    file=sys.stderr,
                )
                after = sequence
            elif kind == "turn.failed":
                print(
                    f"\n[turn failed: {payload.get('code') or 'unknown'}]",
                    file=sys.stderr,
                )
                raise MnerysCliError("turn failed on the Gateway")
            after = max(after, sequence)
        # The Gateway replays buffered events; a fresh turn that produced no
        # frames yet is still running. Poll with a bounded sleep.
        import time
        time.sleep(0.4)
    raise MnerysCliError("stream timed out waiting for a terminal event")


def _summary(client: GatewayClient, turn_id: str) -> dict:
    try:
        snapshot = client.turn_snapshot(turn_id)
        return {"status": snapshot.get("status")}
    except MnerysCliError:
        return {"status": "completed"}


# ---------------------------------------------------------------------------
# serve (local runtime only)
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    """Run the configured local Mnerys server (installed local runtime only)."""
    app_dir = os.environ.get("MNERYS_APP_DIR")
    if not app_dir:
        raise ConfigError(
            "serve requires the local Mnerys runtime. Set MNERYS_APP_DIR to the "
            "installed application directory (remote-only installs do not "
            "include the server).",
        )
    python = os.environ.get("MNERYS_APP_PYTHON") or sys.executable
    command = [
        python, "-m", "uvicorn", "app:app",
        "--host", args.host, "--port", str(args.port),
    ]
    if args.debug:
        command.append("--reload")
    print(f"Starting Mnerys local server on {args.host}:{args.port}")
    print(f"App dir: {app_dir}")
    try:
        result = subprocess.run(command, cwd=app_dir, check=False)
    except KeyboardInterrupt:
        print("\n[server stopped]", file=sys.stderr)
        return 0
    return result.returncode


# ---------------------------------------------------------------------------
# dispatch helpers
# ---------------------------------------------------------------------------

def add_common_parser(subparsers, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--profile", default=None, help="named profile to use")
    return parser