"""Shared-mailbox reads and the subscription tools run on the app-only flow.

The app-only token cache and lock were class attributes of v1's
`MicrosoftGraph`; the port left them behind, so every call with `mailbox=`
and all four subscription tools raised NameError, reported as "Failed to ...".
Through the MCP layer with Microsoft mocked at the transport.
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.outlook_email.server")

HEADERS = {
    "x-microsoft-refresh-token": "rt-1",
    "x-microsoft-client-id": "cid-1",
    "x-microsoft-client-secret": "cs-1",
    "x-microsoft-tenant-id": "tenant-1",
    "x-allowed-mailboxes": "shared@example.com",
}


@pytest.fixture
def requests(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path.endswith("/subscriptions"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(
            200,
            json={
                "id": "m-1",
                "subject": "Hello",
                "from": {"emailAddress": {"address": "a@example.com", "name": "A"}},
                "toRecipients": [],
                "receivedDateTime": "2026-09-20T10:00:00Z",
                "body": {"contentType": "text", "content": "hi"},
                "hasAttachments": False,
            },
        )

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    module._app_only_token_cache.clear()
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))
    return seen


def client_credentials_requests(seen):
    return [
        r
        for r in seen
        if r.url.host == "login.microsoftonline.com" and b"client_credentials" in r.content
    ]


async def test_reading_a_shared_mailbox_uses_the_app_only_token(requests):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(
            "get_email", {"email_id": "m-1", "mailbox": "shared@example.com"}
        )

    assert result.is_error is False, result.content[0].text
    assert client_credentials_requests(requests)
    assert any("/users/shared@example.com/messages/m-1" in str(r.url) for r in requests)


async def test_listing_subscriptions_works(requests):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("list_mailbox_subscriptions", {})

    assert result.is_error is False, result.content[0].text
    assert client_credentials_requests(requests)


async def test_a_mailbox_outside_the_allowlist_is_still_refused(requests):
    """The allowlist is the control on app-only access; fixing the token path
    must not bypass it."""
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(
            "get_email", {"email_id": "m-1", "mailbox": "ceo@example.com"}
        )

    assert result.is_error is True
    assert not any("/users/ceo@example.com" in str(r.url) for r in requests)
