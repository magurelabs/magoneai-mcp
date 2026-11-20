"""Test harness: every tool through a real MCP client, HTTP mocked at the transport.

Nothing above the socket is stubbed. The framework builds `credentials` from
request headers, the real token exchange runs, and every request it and the
tools make lands in `FakeGraph` through an `httpx.MockTransport`. `FakeGraph`
plays both hosts: the Microsoft token endpoint and Graph (plus the
pre-authorized upload and copy-monitor URLs Graph hands out).
"""

import importlib
import json
from collections.abc import Callable
from typing import Any

import httpx
from mcp import Client

module = importlib.import_module("connectors.onedrive.server")
upload_graph = importlib.import_module("connectors.onedrive.tools.upload_graph")
constants = importlib.import_module("connectors.onedrive.tools._constants")

TOKEN_HOST = "login.microsoftonline.com"  # noqa: S105 (a host name)
UPLOAD_URL = "https://contoso-my.sharepoint.com/upload/session-1"
MONITOR_URL = "https://contoso-my.sharepoint.com/monitor/copy-1"

HEADERS = {
    "x-microsoft-refresh-token": "rt-secret",
    "x-microsoft-client-id": "cid-1",
    "x-microsoft-client-secret": "cs-secret",
    "x-microsoft-tenant-id": "tenant-1",
}

Responder = Callable[[httpx.Request], httpx.Response]


class FakeGraph:
    """Routes requests by (method, URL substring) to queued responses.

    A route's last response repeats, so a route with one response answers
    every matching request. The token endpoint is always answered, and its
    requests are kept apart in `token_requests`.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, list[Any]]] = []

    def on(self, method: str, url_part: str, *responses: Any) -> "FakeGraph":
        self._routes.append((method, url_part, list(responses)))
        return self

    def first(self, method: str, url_part: str, *responses: Any) -> "FakeGraph":
        """Like `on`, but matched before every route already added."""
        self._routes.insert(0, (method, url_part, list(responses)))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            self.token_requests.append(request)
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        self.requests.append(request)
        for method, url_part, responses in self._routes:
            if request.method == method and url_part in str(request.url):
                answer = responses.pop(0) if len(responses) > 1 else responses[0]
                return answer(request) if callable(answer) else answer
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def matching(self, method: str, url_part: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and url_part in str(r.url)]


class SessionSim:
    """A Graph upload session: accepts fragments, can fail chosen PUTs once.

    `failures` maps a PUT attempt number (0-based) to the response to send
    instead of accepting that fragment.
    """

    def __init__(self, total: int, failures: dict[int, httpx.Response] | None = None) -> None:
        self.total = total
        self.received = bytearray()
        self.ranges: list[str] = []
        self.failures = dict(failures or {})
        self._attempt = 0

    def put(self, request: httpx.Request) -> httpx.Response:
        attempt, self._attempt = self._attempt, self._attempt + 1
        self.ranges.append(request.headers["Content-Range"])
        if attempt in self.failures:
            return self.failures[attempt]
        start = int(request.headers["Content-Range"].split()[1].split("-")[0])
        assert start == len(self.received), "fragment does not continue the upload"
        self.received.extend(request.content)
        if len(self.received) >= self.total:
            return httpx.Response(201, json=drive_item("big.bin", self.total))
        return httpx.Response(202, json={"nextExpectedRanges": [f"{len(self.received)}-"]})

    def status(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nextExpectedRanges": [f"{len(self.received)}-"]})


def install(monkeypatch: Any, graph: FakeGraph, headers: dict | None = None) -> list[float]:
    """Route every httpx client through `graph`; return the list of recorded waits."""
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(graph)
        return real_client(*args, **kwargs)

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    sent = dict(HEADERS if headers is None else headers)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(upload_graph, "sleep", record)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(sent))
    monkeypatch.delenv("ONEDRIVE_MAX_UPLOAD_MB", raising=False)
    return waits


async def call(tool: str, arguments: dict | None = None) -> tuple[dict, bool]:
    """Call `tool` over MCP; return (the JSON result, isError)."""
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, arguments or {})
    return json.loads(result.content[0].text), result.is_error


async def run(monkeypatch: Any, tool: str, graph: FakeGraph, **arguments: Any) -> dict:
    """Install `graph`, call `tool` with `arguments`, return its JSON result."""
    install(monkeypatch, graph)
    result, _ = await call(tool, arguments)
    return result


def respond(body: Any = None, status: int = 200, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, **kwargs)


def graph_answering(method: str, url_part: str, body: Any = None, status: int = 200) -> FakeGraph:
    return FakeGraph().on(method, url_part, respond(body, status))


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def file_item(id_: str = "1", name: str = "a.txt", **extra: Any) -> dict[str, Any]:
    """A Graph driveItem for a file."""
    item = {
        "id": id_,
        "name": name,
        "file": {"mimeType": "text/plain"},
        "size": 42,
        "lastModifiedDateTime": "2026-01-01T00:00:00Z",
        "@microsoft.graph.downloadUrl": f"https://dl.example/{id_}",
    }
    item.update(extra)
    return item


def folder_item(id_: str = "F1", name: str = "Docs", child_count: int = 3, **extra: Any) -> dict:
    """A Graph driveItem for a folder."""
    item = {
        "id": id_,
        "name": name,
        "folder": {"childCount": child_count},
        "size": 0,
        "lastModifiedDateTime": "2026-01-01T00:00:00Z",
    }
    item.update(extra)
    return item


def drive_item(name: str = "a.txt", size: int = 1, **extra: Any) -> dict[str, Any]:
    """The driveItem an upload returns."""
    item = {
        "id": "ITEM1",
        "name": name,
        "size": size,
        "webUrl": f"https://onedrive.example/{name}",
        "parentReference": {"id": "PARENT1", "path": "/drive/root:/Reports"},
    }
    item.update(extra)
    return item
