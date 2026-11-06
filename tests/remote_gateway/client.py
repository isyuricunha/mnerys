"""Independent scripted remote Gateway client (M2.4 transport proof).

This module imports only the standard library and httpx. It never imports
the Web server, the session database or the Agent Loop: it only speaks the
versioned HTTP contract, so the same client works against any Gateway
deployment with the same contract.
"""

from dataclasses import dataclass
import json
from typing import Any, Optional

import httpx


@dataclass(frozen=True)
class TurnCreated:
    turn_id: str
    session_id: str
    status: str


@dataclass(frozen=True)
class TurnSnapshot:
    turn_id: str
    session_id: str
    status: str
    terminal: str | None


class RemoteGatewayError(RuntimeError):
    """A non-2xx Gateway response, with the sanitized detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Gateway {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class RemoteGatewayClient:
    """The minimal scripted client for the versioned Gateway API v1.

    One shared httpx surface: production constructs its own client from the
    base URL, and acceptance tests inject the same in-process app transport
    (a TestClient) without changing any request code.
    """

    def __init__(
        self, base_url: str = "", token: str = "", *, timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        self._client = client if client is not None else httpx.Client(
            base_url=base_url, timeout=timeout,
        )
        self._headers = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, json_body=None,
                 params=None) -> Any:
        response = self._client.request(
            method, path, headers=self._headers, json=json_body, params=params,
        )
        if response.status_code >= 400:
            detail = response.text[:200] if response.text else "Gateway error"
            raise RemoteGatewayError(response.status_code, detail)
        return response.json()

    def create_turn(
        self, session_id: str, message: str, *, mode: str = "chat",
        idempotency_key: str | None = None,
    ) -> TurnCreated:
        body = {
            "session_id": session_id, "message": message, "mode": mode,
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        data = self._request("POST", "/api/v1/turns", json_body=body)
        return TurnCreated(
            turn_id=data["turn_id"], session_id=data["session_id"],
            status=data["status"],
        )

    def turn(self, turn_id: str) -> TurnSnapshot:
        data = self._request("GET", f"/api/v1/turns/{turn_id}")
        return TurnSnapshot(
            turn_id=data["turn_id"], session_id=data.get("session_id", ""),
            status=data["status"], terminal=data.get("terminal"),
        )

    def events(self, turn_id: str, after_seq: int = 0) -> list[dict]:
        response = self._client.get(
            f"/api/v1/turns/{turn_id}/events",
            headers=self._headers, params={"after_seq": after_seq},
        )
        if response.status_code >= 400:
            detail = response.text[:200] if response.text else "Gateway error"
            raise RemoteGatewayError(response.status_code, detail)
        items = []
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            items.append(json.loads(payload))
        return items

    def cancel(self, turn_id: str, session_id: str) -> dict:
        return self._request(
            "POST", f"/api/v1/turns/{turn_id}/cancel",
            json_body={"session_id": session_id},
        )

    def decide_approval(
        self, approval_id: str, session_id: str, decision: str,
    ) -> dict:
        return self._request(
            "POST", f"/api/v1/approvals/{approval_id}/decision",
            params={"session_id": session_id},
            json_body={"decision": decision},
        )

    def sessions(self) -> list[dict]:
        return self._request("GET", "/api/v1/sessions")["sessions"]

    def models(self) -> list[dict]:
        return self._request("GET", "/api/v1/models")["models"]

    def agents(self) -> list[dict]:
        return self._request("GET", "/api/v1/agents")["agents"]

    def tools(self) -> list[str]:
        return self._request("GET", "/api/v1/tools")["tools"]