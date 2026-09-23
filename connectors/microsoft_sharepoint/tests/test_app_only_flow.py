"""Every SharePoint tool runs on the app-only flow, through the MCP layer.

Two port defects stopped all of them. The token cache and its lock were
class attributes of v1's `MicrosoftGraph` and were left behind, so every tool
raised NameError on its first call; and `GraphNotFoundError` was raised but
never defined, so every Graph 404 became a NameError too.
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.microsoft_sharepoint.server")

HEADERS = {
    "x-microsoft-client-id": "cid-1",
    "x-microsoft-client-secret": "cs-1",
    "x-microsoft-tenant-id": "tenant-1",
    "x-sharepoint-site-id": "contoso.sharepoint.com,site-guid,web-guid",
}


def install(monkeypatch, graph_status: int = 200):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "app-token", "expires_in": 3600})
        if graph_status != 200:
            return httpx.Response(graph_status, json={"error": {"code": "itemNotFound"}})
        return httpx.Response(200, json={"value": [{"id": "d-1", "name": "Documents"}]})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    module._app_only_token_cache.clear()
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))
    return requests


async def test_a_tool_gets_an_app_only_token_and_reaches_graph(monkeypatch):
    requests = install(monkeypatch)

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("sharepoint_site_libraries", {})

    assert result.is_error is False, result.content[0].text
    graph = [r for r in requests if r.url.host == "graph.microsoft.com"]
    assert graph and graph[0].headers["authorization"] == "Bearer app-token"


async def test_the_token_is_reused_across_calls(monkeypatch):
    requests = install(monkeypatch)

    async with Client(module.server.mcp) as client:
        await client.call_tool("sharepoint_site_libraries", {})
        await client.call_tool("sharepoint_site_libraries", {})

    assert sum(r.url.host == "login.microsoftonline.com" for r in requests) == 1


async def test_a_graph_404_is_reported_as_not_found(monkeypatch):
    install(monkeypatch, graph_status=404)

    async with Client(module.server.mcp) as client:
        result = await client.call_tool(
            "sharepoint_get_drive_item", {"drive_id": "d-1", "item_id": "missing"}
        )

    text = result.content[0].text
    assert result.is_error is True
    assert "NameError" not in text and "not defined" not in text
    assert "not found" in text.lower()


async def test_only_the_tools_v1_served_are_offered():
    async with Client(module.server.mcp) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)

    assert names == [
        "sharepoint_download_file",
        "sharepoint_get_drive_item",
        "sharepoint_get_site",
        "sharepoint_list_children",
        "sharepoint_list_folder",
        "sharepoint_site_libraries",
    ]
