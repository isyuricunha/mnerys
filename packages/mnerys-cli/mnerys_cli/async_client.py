"""Async Gateway API client for the interactive TUI.

Transport only, like the synchronous :class:`mnerys_cli.client.GatewayClient`:
it never imports the server, the database or the agent loop, and it never
renders decisions for approvals. The TUI runs inside Textual's asyncio loop,
so every network operation is awaitable and every SSE response is consumed
incrementally as the server yields it.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx

from mnerys_cli.errors import AuthError, MnerysCliError


class AsyncGatewayClient:
    """Awaitable mirror of the Gateway API v1 surface used by the TUI."""

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
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(
        self, method: str, path: str, *, json_body=None, params=None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self.endpoint + path, headers=self._headers(),
                json=json_body, params=params,
            )
        except httpx.TransportError as exc:
            raise MnerysCliError(f"Could not reach {self.endpoint}: {exc}") from exc
        return _raise_for_status(response, self.endpoint)

    async def health(self) -> tuple[int, str]:
        """Plain health check; returns (status_code, body prefix)."""
        try:
            response = await self._client.get(
                self.endpoint + "/api/health", timeout=10,
            )
            return response.status_code, response.text[:80]
        except httpx.TransportError as exc:
            return 0, str(exc)

    # ------------------------------------------------------------------
    # Gateway API v1
    # ------------------------------------------------------------------

    async def create_turn(
        self, session_id: str, message: str, *, mode: str = "chat",
        idempotency_key: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "session_id": session_id, "message": message, "mode": mode,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        response = await self._request("POST", "/api/v1/turns", json_body=body)
        return response.json()

    async def turn_snapshot(self, turn_id: str) -> dict:
        response = await self._request("GET", f"/api/v1/turns/{turn_id}")
        return response.json()

    async def turn_events(
        self, turn_id: str, after_seq: int = 0,
    ) -> AsyncIterator[dict]:
        """Yield sanitized SSE events incrementally until ``[DONE]``.

        Each yielded item is ``{"sequence", "kind", "payload"}``. A malformed
        frame is a contract violation and is skipped, never crashing the UI.
        """
        request = self._client.build_request(
            "GET", self.endpoint + f"/api/v1/turns/{turn_id}/events",
            headers=self._headers(), params={"after_seq": after_seq},
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TransportError as exc:
            raise MnerysCliError(f"Could not reach {self.endpoint}: {exc}") from exc
        try:
            _raise_for_status(response, self.endpoint)
        except Exception:
            await response.aclose()
            raise
        try:
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    item = _parse_sse_frame(frame)
                    if item is None:
                        continue
                    if not isinstance(item, dict):
                        # The [DONE] terminator ends the replay window; the
                        # caller re-polls until the turn snapshot is terminal.
                        return
                    yield item
        except (httpx.TransportError, httpx.StreamError) as exc:
            # An aborted connection (server restart, proxy drop) surfaces as
            # StreamError/ResponseNotRead, not as TransportError. The pump
            # treats this as an interruption and reconnects with the cursor.
            raise MnerysCliError(f"Gateway stream interrupted: {exc}") from exc
        finally:
            try:
                await response.aclose()
            except (httpx.TransportError, httpx.StreamError):
                # The peer already died; there is nothing left to close.
                pass

    async def cancel_turn(self, turn_id: str, session_id: str) -> dict:
        response = await self._request(
            "POST", f"/api/v1/turns/{turn_id}/cancel",
            json_body={"session_id": session_id},
        )
        return response.json()

    async def sessions(self) -> list[dict]:
        response = await self._request("GET", "/api/v1/sessions")
        return (response.json() or {}).get("sessions", [])

    async def create_session(
        self, *, name: str = "", endpoint_id: str | None = None,
        model: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name}
        if endpoint_id:
            body["endpoint_id"] = endpoint_id
        if model:
            body["model"] = model
        response = await self._request("POST", "/api/v1/sessions", json_body=body)
        return response.json()

    async def session_detail(
        self, session_id: str, *, limit: int = 200, offset: int = 0,
    ) -> dict:
        response = await self._request(
            "GET", f"/api/v1/sessions/{session_id}",
            params={"limit": limit, "offset": offset},
        )
        return response.json()

    async def update_session(
        self, session_id: str, *, name: str | None = None,
        model: str | None = None, endpoint_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if model is not None:
            body["model"] = model
        if endpoint_id is not None:
            body["endpoint_id"] = endpoint_id
        response = await self._request(
            "PATCH", f"/api/v1/sessions/{session_id}", json_body=body,
        )
        return response.json()

    async def session_approvals(self, session_id: str) -> list[dict]:
        response = await self._request(
            "GET", f"/api/v1/sessions/{session_id}/approvals",
        )
        return (response.json() or {}).get("approvals", [])

    async def approval_preview(
        self, approval_id: str, session_id: str,
    ) -> dict:
        response = await self._request(
            "GET", f"/api/v1/approvals/{approval_id}",
            params={"session_id": session_id},
        )
        return response.json()

    async def decide_approval(
        self, approval_id: str, session_id: str, decision: str,
    ) -> dict:
        response = await self._request(
            "POST", f"/api/v1/approvals/{approval_id}/decision",
            params={"session_id": session_id},
            json_body={"decision": decision},
        )
        return response.json()

    async def models(self) -> list[dict]:
        response = await self._request("GET", "/api/v1/models")
        return (response.json() or {}).get("models", [])

    async def agents(self) -> list[dict]:
        response = await self._request("GET", "/api/v1/agents")
        return (response.json() or {}).get("agents", [])

    async def tools(self) -> list[str]:
        response = await self._request("GET", "/api/v1/tools")
        return (response.json() or {}).get("tools", [])

    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        """Async context-manager support for scoped TUI sessions."""
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()


def _raise_for_status(response: httpx.Response, endpoint: str) -> httpx.Response:
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


def _parse_sse_frame(frame: str) -> dict | str | None:
    """Parse one SSE frame into an event item, ``"DONE"`` or None."""
    payload: str | None = None
    for line in frame.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
        elif line.startswith("data:"):
            payload = line[5:].lstrip(" ")
    if payload is None:
        return None
    if payload.strip() == "[DONE]":
        return "DONE"
    try:
        item = json.loads(payload)
    except ValueError:
        # The server serializes sanitized payloads; a malformed frame
        # is evidence of a contract violation and must not crash the UI.
        return None
    return item if isinstance(item, dict) else None


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])[:200]
    except ValueError:
        pass
    return response.text[:200]
