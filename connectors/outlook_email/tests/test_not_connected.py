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


async def test_graph_403_reports_missing_permission(monkeypatch):
    install(monkeypatch, graph_says(403, {"error": {"message": "Mail.Read not consented"}}))

    result = await search()

    error = payload(result)["error"]
    assert error.startswith("Failed to search emails: Access denied.")
    assert "permission" in error


async def get_shared(email_id: str = "m-1"):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(
            "get_email", {"email_id": email_id, "mailbox": "shared@example.com"}
        )


async def test_shared_mailbox_403_reports_the_consent_gap(monkeypatch):
    install(monkeypatch, graph_says(403, {"error": {"message": "Access is denied."}}))

    result = await get_shared()

    error = payload(result)["error"]
    assert error.startswith("Failed to get email: Access denied. Verify the app has")
    assert error.endswith("(Access is denied.)")


async def test_shared_mailbox_401_reports_credentials(monkeypatch):
    install(monkeypatch, graph_says(401, {"error": {"message": "InvalidAuthenticationToken"}}))

    result = await get_shared()

    assert payload(result)["error"].startswith(
        "Failed to get email: Authentication failed. Check app credentials"
    )


async def test_shared_mailbox_without_tenant_reports_it(monkeypatch):
    headers = {k: v for k, v in HEADERS.items() if k != "x-microsoft-tenant-id"}
    install(monkeypatch, graph_says(200), headers=headers)

    result = await get_shared()

    assert payload(result)["error"] == (
        "Failed to get email: Missing tenant_id. App-only OAuth requires an explicit tenant id."
    )


def token_endpoint_says(monkeypatch, status: int, **response):
    real = httpx.AsyncClient

    def rejecting(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(status, **response)
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", rejecting)


async def test_shared_mailbox_rejected_app_token_reports_the_reason(monkeypatch):
    install(monkeypatch, graph_says(200))
    token_endpoint_says(
        monkeypatch, 401, json={"error_description": "AADSTS7000215: Invalid client secret."}
    )

    result = await get_shared()

    assert payload(result)["error"] == (
        "Failed to get email: App-only token acquisition failed (401): "
        "AADSTS7000215: Invalid client secret."
    )


async def test_raw_token_endpoint_body_never_reaches_the_caller(monkeypatch):
    install(monkeypatch, graph_says(200))
    token_endpoint_says(monkeypatch, 502, text="<html>upstream proxy page with internals</html>")

    result = await search()

    error = payload(result)["error"]
    assert error == "Failed to search emails: Token refresh failed (502): no reason given"
    assert "internals" not in error


async def test_long_reason_is_cut_to_one_short_line(monkeypatch):
    install(monkeypatch, graph_says(200))
    reason = "AADSTS70008: expired. " + "x" * 600 + "\nTrace ID: 1234"
    token_endpoint_says(monkeypatch, 400, json={"error_description": reason})

    result = await search()

    error = payload(result)["error"]
    assert "Trace ID" not in error
    assert error.endswith("...")
    assert len(error) < 300
