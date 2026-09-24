"""Every DocuSign tool authenticates through `_esign_auth`, which caches the
access token per connection. The cache and its lock were class attributes of
the v1 auth mixin; the port flattened the methods and left the attributes
behind, so the first call to any of the 14 tools raised NameError.

Through the MCP layer, with DocuSign mocked at the transport.
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.docusign.server")

HEADERS = {
    "x-docusign-refresh-token": "rt-1",
    "x-docusign-client-id": "cid-1",
    "x-docusign-client-secret": "cs-1",
}
ACCOUNT = "a1b2c3d4-0000-0000-0000-000000000001"


@pytest.fixture
def seen(monkeypatch):
    module._token_cache.clear()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if path.endswith("/oauth/userinfo"):
            return httpx.Response(
                200,
                json={
                    "sub": "u-1",
                    "accounts": [
                        {"account_id": ACCOUNT, "base_uri": "https://demo.docusign.net", "is_default": True}
                    ],
                },
            )
        return httpx.Response(200, json={"envelopes": [], "resultSetSize": "0"})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))
    return requests


async def test_a_tool_authenticates_and_reaches_the_api(seen):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("list_envelopes", {})

    assert result.is_error is False, result.content[0].text
    assert any(f"/restapi/v2.1/accounts/{ACCOUNT}/envelopes" in str(r.url) for r in seen)


async def test_a_second_call_reuses_the_token(seen):
    async with Client(module.server.mcp) as client:
        await client.call_tool("list_envelopes", {})
        await client.call_tool("list_envelopes", {})

    exchanges = [r for r in seen if r.url.path.endswith("/oauth/token")]
    assert len(exchanges) == 1


def test_the_cache_is_not_keyed_by_the_raw_token():
    key = module._cache_key("cid-1", "rt-1")

    assert "rt-1" not in key
    assert key != module._cache_key("cid-1", "rt-2")
