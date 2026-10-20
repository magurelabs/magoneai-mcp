"""A mailbox with no usable Microsoft token fails like the calendar connector:
"<action>: <reason>" carrying the credential message, not a bare "Failed to ...".

Through a real MCP client, with Graph mocked at the transport.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import HEADERS, install, module, payload

pytestmark = pytest.mark.requires_connectors


async def search(arguments: dict | None = None):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("search_emails", arguments or {"query": "invoice"})


def graph_says(status: int, body: dict | None = None):
    return lambda request: httpx.Response(status, json=body or {})


async def test_no_token_reports_the_missing_token(monkeypatch):
    install(monkeypatch, graph_says(200), headers={})

    result = await search()

    assert payload(result) == {
        "success": False,
        "error": "Failed to search emails: Missing X-Microsoft-Refresh-Token header",
    }


async def test_no_token_on_another_tool_too(monkeypatch):
    install(monkeypatch, graph_says(200), headers={})

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("list_emails", {})

    assert payload(result)["error"].startswith(
        "Failed to list emails: Missing X-Microsoft-Refresh-Token"
    )


async def test_graph_401_reports_reauthenticate(monkeypatch):
    install(monkeypatch, graph_says(401, {"error": {"message": "InvalidAuthenticationToken"}}))

    result = await search()

    assert payload(result) == {
        "success": False,
        "error": "Failed to search emails: Authentication failed. Please re-authenticate.",
    }


async def test_rejected_refresh_reports_the_reason(monkeypatch):
    real = httpx.AsyncClient
    install(monkeypatch, graph_says(200))

    def rejecting(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error_description": "AADSTS70008: expired"})
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", rejecting)

    result = await search()

    assert payload(result)["error"].startswith(
        "Failed to search emails: Token refresh failed (400)"
    )


async def test_other_graph_errors_stay_opaque(monkeypatch):
    install(monkeypatch, graph_says(500, {"error": {"message": "internal detail"}}))

    result = await search()

    assert payload(result) == {"success": False, "error": "Failed to search emails"}


async def test_success_is_unchanged(monkeypatch):
    message = {
        "id": "m-1",
        "subject": "Invoice",
        "from": {"emailAddress": {"name": "A", "address": "a@example.com"}},
        "receivedDateTime": "2026-01-02T03:04:05Z",
        "isRead": False,
        "bodyPreview": "hi",
        "hasAttachments": False,
    }
    install(monkeypatch, graph_says(200, {"value": [message]}), headers=HEADERS)

    result = await search()

    data = payload(result)
    assert data["success"] is True
    assert data["count"] == 1
    assert data["emails"][0]["id"] == "m-1"
