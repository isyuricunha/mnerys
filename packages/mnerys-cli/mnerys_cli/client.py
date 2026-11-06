"""The authenticated Gateway API client used by every mnerys command.

This is transport only: it never imports the server, the database or the
agent loop, and it never renders decisions for approvals.
"""

import json
from typing import Any

import httpx

from mnerys_cli.errors import AuthError, MnerysCliError

EVENTS_URL = "/api/v1/turns/{turn_id}/events"


class GatewayClient:
    def __init__(
        self, endpoint: str, token: str | None, *, timeout: float = 60.0,
        transport: Any = None,
    ):
        if not endpoint.startswith(("http://", "https://")):
            raise MnerysCliError(
                f"Invalid endpoint {endpoint!r}: must be http(s)://",
            )
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self._timeout = timeout
        # ``transport`` exists only so tests can point the same client at an
        # in-process ASGI/Mock transport; production uses real HTTP.
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method: str, path: str, *, json_body=None, params=None) -> httpx.Response:
        try:
            response = self._client.request(
                method, self.endpoint + path, headers=self._headers(),
                json=json_body, params=params,
            )
        except httpx.TransportError as exc:
            raise MnerysCliError(f"Could not reach {self.endpoint}: {exc}") from exc
        if response.status_code in (401, 403):
            detail = _detail(response)
            raise AuthError(
                f"Gateway rejected the request ({response.status_code}). "
                f"{detail or 'The token is missing, invalid or not authorized.'}",
            )
        if response.status_code >= 400:
            raise MnerysCliError(
                f"Gateway error {response.status_code}: {_detail(response)}",
            )
        return response

    def health(self) -> tuple[int, str]:
        """Plain health check; returns (status_code, body prefix)."""
        try:
            response = self._client.get(self.endpoint + "/api/health", timeout=10)
            return response.status_code, response.text[:80]
        except httpx.TransportError as exc:
            return 0, str(exc)

    # ------------------------------------------------------------------
    # Gateway API v1
    # ------------------------------------------------------------------

    def create_turn(
        self, session_id: str, message: str, *, mode: str = "chat",
        idempotency_key: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "session_id": session_id, "message": message, "mode": mode,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        response = self._request("POST", "/api/v1/turns", json_body=body)
        return response.json()

    def turn_snapshot(self, turn_id: str) -> dict:
        response = self._request("GET", f"/api/v1/turns/{turn_id}")
        return response.json()

    def turn_events(self, turn_id: str, after_seq: int = 0) -> list[dict]:
        """Fetch and parse the sanitized SSE event stream."""
        response = self._request(
            "GET", f"/api/v1/turns/{turn_id}/events",
            params={"after_seq": after_seq},
        )
        items: list[dict] = []
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                items.append(json.loads(payload))
            except ValueError:
                # The server serializes sanitized payloads; a malformed frame
                # is evidence of a contract violation and must not crash.
                continue
        return items

    def cancel_turn(self, turn_id: str, session_id: str) -> dict:
        response = self._request(
            "POST", f"/api/v1/turns/{turn_id}/cancel",
            json_body={"session_id": session_id},
        )
        return response.json()

    def sessions(self) -> list[dict]:
        response = self._request("GET", "/api/v1/sessions")
        return (response.json() or {}).get("sessions", [])

    def models(self) -> list[dict]:
        response = self._request("GET", "/api/v1/models")
        return (response.json() or {}).get("models", [])

    def agents(self) -> list[dict]:
        response = self._request("GET", "/api/v1/agents")
        return (response.json() or {}).get("agents", [])

    def tools(self) -> list[str]:
        response = self._request("GET", "/api/v1/tools")
        return (response.json() or {}).get("tools", [])

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GatewayClient":
        """Context-manager support: ``with GatewayClient(...) as client``."""
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])[:200]
    except ValueError:
        pass
    return response.text[:200]