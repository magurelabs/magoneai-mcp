"""reply_email says why the Graph call failed instead of a bare "Failed to reply to email".

A failed customer acknowledgment used to read only "Failed to reply to email" in the MagOneAI run,
with the cause (message not found, access denied, ...) visible only in the pod's log. The Graph
helpers raise ValueError with a message written for the caller, so that text is passed through; any
other exception stays generic because its text is not vetted.

Through a real MCP client, with Graph mocked at the transport.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import install, module, payload

pytestmark = pytest.mark.requires_connectors


async def reply(arguments: dict | None = None):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(
            "reply_email", {"email_id": "m-1", "body": "hi", **(arguments or {})}
        )


async def test_a_missing_message_says_so(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(404, json={"error": {"message": "gone"}}))

    result = await reply()

    assert result.is_error is True
    assert payload(result)["error"] == "Failed to reply to email: Resource not found."


async def test_a_graph_error_carries_its_status_and_message(monkeypatch):
    install(
        monkeypatch, lambda r: httpx.Response(400, json={"error": {"message": "bad recipient"}})
    )

    result = await reply()

    assert result.is_error is True
    error = payload(result)["error"]
    assert error.startswith("Failed to reply to email: ")
    assert "400" in error and "bad recipient" in error


async def test_an_unexpected_exception_stays_generic(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("internal detail that must not leak")

    install(monkeypatch, boom)

    result = await reply()

    assert result.is_error is True
    assert payload(result)["error"] == "Failed to reply to email"
