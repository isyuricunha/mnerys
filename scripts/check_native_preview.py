#!/usr/bin/env python3
"""Safe, unauthenticated smoke test for a private native Mnerys preview."""
from __future__ import annotations

import argparse
import json
from urllib import error, parse, request


def check_preview(base_url: str, timeout: float = 10) -> dict:
    url = base_url.rstrip("/")
    parsed = parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise ValueError("Supply only a trusted http(s) base URL, without credentials or a path")
    if timeout <= 0:
        raise ValueError("Timeout must be positive")

    def get(path: str):
        try:
            with request.urlopen(request.Request(url + path, headers={"Accept": "application/json" if path.startswith("/api/") else "text/html"}), timeout=timeout) as resp:
                return resp.status, resp.read(128_000)
        except error.HTTPError as exc:
            return exc.code, exc.read(4096)

    status, health_bytes = get("/api/health")
    if status != 200 or json.loads(health_bytes).get("status") != "healthy":
        raise RuntimeError("Health check failed")

    status, login_bytes = get("/login")
    if status != 200 or b"Mnerys" not in login_bytes:
        raise RuntimeError("Mnerys login HTML not available")

    status, _ = get("/api/ready")
    if status not in (401, 403):
        raise RuntimeError("Readiness endpoint is unexpectedly public or not reachable")

    return {"health": "ok", "login": "ok", "readiness_auth": "enforced"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a trusted Mnerys private preview without logging secrets")
    parser.add_argument("--base-url", required=True, help="Private Tailscale address; not printed")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    result = check_preview(args.base_url, args.timeout)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
