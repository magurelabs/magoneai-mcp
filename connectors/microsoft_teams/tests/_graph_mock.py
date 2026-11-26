"""A Microsoft Graph stand-in at the httpx transport, shared by these tests.

Every tool call goes through a real MCP client and the connector's own Graph
helpers; only the network is replaced. The token endpoint answers with a fixed
token, every Graph request is recorded and answered by a per-test router, and
the retry sleep is recorded instead of slept.
"""

import importlib
import json
from collections.abc import Callable

import httpx

module = importlib.import_module("connectors.microsoft_teams.server")
shared = importlib.import_module("connectors.microsoft_teams._shared")

HEADERS = {
    "x-microsoft-refresh-token": "rt-1",
    "x-microsoft-client-id": "cid-1",
    "x-microsoft-client-secret": "cs-1",
    "x-microsoft-tenant-id": "tenant-1",
}

GRAPH = "https://graph.microsoft.com/v1.0"

Router = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """What one test sent to Graph, and how long it was asked to wait."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []


def install(monkeypatch, router: Router, headers: dict[str, str] | None = None) -> Recorder:
    """Answer Graph with `router`; return what was sent."""
    seen = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "user-token", "expires_in": 3600})
        seen.requests.append(request)
        return router(request)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def no_sleep(seconds: float) -> None:
        seen.sleeps.append(seconds)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(shared.asyncio, "sleep", no_sleep)
    sent = dict(HEADERS if headers is None else headers)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(sent))
    return seen


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def message(msg_id: str, created: str, **extra) -> dict:
    """A Graph chatMessage with the fields the paged tools read."""
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": created,
        "lastModifiedDateTime": extra.pop("modified", created),
        "deletedDateTime": None,
        "body": {"contentType": "html", "content": f"<p>{msg_id}</p>"},
        **extra,
    }
