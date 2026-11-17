"""JSON-RPC over Streamable HTTP to the official Playwright MCP server.

The upstream is stateful: ``initialize`` returns an ``Mcp-Session-Id`` that
every later request must carry, and a request without one is refused ("Server
not initialized"). Replies come back as JSON or as a server-sent event stream,
depending on the upstream; both are parsed here.

Only the headers the protocol needs are sent. Nothing from the caller's
request (service token, bearer token, credential or routing headers) ever
reaches the browser.

A client is opened per request rather than held. The upstream is on a private
network next to this connector, so a connection costs little, and a held
client is bound to the event loop that opened it.
"""

from __future__ import annotations

import itertools
import json

import httpx

PROTOCOL_VERSION = "2025-06-18"
ACCEPT = "application/json, text/event-stream"
# Fallbacks only; the gateway passes the connector's real SERVER_NAME and
# SERVER_VERSION so the upstream's logs name the client that is actually
# calling it, at the version it is actually running.
DEFAULT_CLIENT_NAME = "magone-mcp-playwright"
CONNECT_TIMEOUT_SECONDS = 10.0
_GONE_STATUSES = (400, 404)


class UpstreamError(Exception):
    """The browser service failed or refused. The message is shown to the model."""


class SessionGone(UpstreamError):
    """The upstream no longer knows this session id."""


def sse_messages(text: str) -> list[dict]:
    """JSON-RPC messages from an event stream: one per event's joined ``data:`` lines."""
    messages, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            try:
                messages.append(json.loads("\n".join(data)))
            except json.JSONDecodeError:
                pass
            data = []
    return messages


def parse_reply(resp: httpx.Response, request_id: int) -> dict:
    """The JSON-RPC message answering ``request_id``."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        messages = sse_messages(resp.text)
    else:
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamError("The browser service sent an unreadable reply.") from None
        messages = body if isinstance(body, list) else [body]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise UpstreamError("The browser service sent no reply to the request.")


class UpstreamRpc:
    """Stateless transport: callers own session ids (see upstream_sessions.py)."""

    def __init__(
        self,
        url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        client_name: str | None = None,
        client_version: str | None = None,
    ) -> None:
        self.url = url
        self.timeout = httpx.Timeout(timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
        # Tests pass an httpx.MockTransport; production uses the default one.
        self.transport = transport
        self.client_info = {
            "name": client_name or DEFAULT_CLIENT_NAME,
            "version": client_version or "0.0.0",
        }
        self._ids = itertools.count(1)

    def _headers(self, session_id: str | None, protocol: str | None) -> dict:
        headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        return headers

    async def _send(self, method: str, headers: dict, payload: dict | None) -> httpx.Response:
        kwargs: dict = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, follow_redirects=False
            ) as client:
                return await client.request(method, self.url, **kwargs)
        except httpx.TimeoutException:
            raise UpstreamError(
                f"The browser did not answer within {self.timeout.read:.0f} seconds."
            ) from None
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"The Playwright browser service is unreachable ({type(exc).__name__})."
            ) from None

    async def request(
        self, method: str, params: dict, session_id: str | None, protocol: str | None
    ) -> dict:
        """Send one request and return its ``result``."""
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        resp = await self._send("POST", self._headers(session_id, protocol), payload)
        if session_id and resp.status_code in _GONE_STATUSES:
            raise SessionGone(f"Browser session expired (HTTP {resp.status_code}).")
        if resp.status_code != 200:
            raise UpstreamError(f"The browser service failed (HTTP {resp.status_code}).")
        message = parse_reply(resp, request_id)
        if "error" in message:
            error = message.get("error") or {}
            raise UpstreamError(f"Browser error: {str(error.get('message', error))[:500]}")
        return message.get("result") or {}

    async def initialize(self) -> tuple[str | None, str]:
        """Open a session: ``(session_id, protocol_version)``."""
        request_id = next(self._ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": self.client_info,
            },
        }
        resp = await self._send("POST", self._headers(None, None), payload)
        if resp.status_code != 200:
            raise UpstreamError(f"The browser service refused to start (HTTP {resp.status_code}).")
        message = parse_reply(resp, request_id)
        if "error" in message:
            raise UpstreamError("The browser service refused to start a session.")
        session_id = resp.headers.get("mcp-session-id")
        protocol = str((message.get("result") or {}).get("protocolVersion") or PROTOCOL_VERSION)
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        await self._send("POST", self._headers(session_id, protocol), note)
        return session_id, protocol

    async def delete(self, session_id: str | None, protocol: str | None) -> None:
        """End a session so the upstream closes its browser. Failures are ignored."""
        if not session_id:
            return
        try:
            await self._send("DELETE", self._headers(session_id, protocol), None)
        except UpstreamError:
            pass
