"""A Microsoft Graph stand-in at the httpx transport, shared by these tests.

Every tool call goes through a real MCP client and the connector's own Graph
helpers; only the network is replaced. Token endpoints answer with a fixed
token, and every Graph request is recorded and answered by a per-test router.
"""

import importlib
import json
from collections.abc import Callable

import httpx

module = importlib.import_module("connectors.outlook_email.server")

HEADERS = {
    "x-microsoft-refresh-token": "rt-1",
    "x-microsoft-client-id": "cid-1",
    "x-microsoft-client-secret": "cs-1",
    "x-microsoft-tenant-id": "tenant-1",
    "x-allowed-mailboxes": "shared@example.com",
}

Router = Callable[[httpx.Request], httpx.Response]


def install(monkeypatch, router: Router, headers: dict[str, str] | None = None):
    """Answer Graph with `router`; return the list of Graph requests seen."""
    graph: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            token = "app-token" if b"client_credentials" in request.content else "user-token"
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        graph.append(request)
        return router(request)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    module._app_only_token_cache.clear()
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    sent = dict(HEADERS if headers is None else headers)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(sent))
    return graph


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


def payload(result) -> dict:
    return json.loads(result.content[0].text)
