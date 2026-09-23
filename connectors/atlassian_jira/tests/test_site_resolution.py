"""Every tool that reaches the site goes through `request()`, which resolves the
cloud id first. A second, two-argument `resolve_cloud_id` carried across from
the v1 gateway shadowed the one-argument wrapper, so `request()` raised
TypeError before any HTTP call and every site-bound tool failed (all but the
two that only list accessible resources).

Called through the MCP layer with the vendor mocked at the transport, so the
failure mode this pins is exactly the one a caller saw.
"""

import importlib
import json

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.atlassian_jira.server")

HEADERS = {
    "x-atlassian-access-token": "at-1",
    "x-atlassian-cloud-id": "c-1",
    "x-atlassian-site-url": "https://acme.atlassian.net",
}


@pytest.fixture
def seen(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/accessible-resources"):
            return httpx.Response(
                200, json=[{"id": "c-1", "url": "https://acme.atlassian.net", "scopes": []}]
            )
        return httpx.Response(200, json=json.loads('{"key": "PROJ-1", "fields": {"summary": "s"}}'))

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(
        "magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS)
    )
    return requests


async def test_a_site_bound_tool_reaches_the_site(seen):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("getJiraIssue", json.loads('{"cloudId": "c-1", "issueIdOrKey": "PROJ-1"}'))

    assert result.is_error is False, result.content[0].text
    assert any("/ex/jira/c-1/rest/api/3/issue/PROJ-1" in str(r.url) for r in seen), [str(r.url) for r in seen]


async def test_the_wrapper_takes_credentials_only():
    """The shadowing, stated directly: the public name takes one argument."""
    import inspect

    assert list(inspect.signature(module.resolve_cloud_id).parameters) == ["credentials"]
