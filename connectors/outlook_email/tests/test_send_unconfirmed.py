"""A send that fails in transit keeps its draft and says to check Sent Items.

A timeout or a dropped connection on /send leaves the outcome unknown: Graph
may have sent the message, and the draft id is then the sent message. Deleting
it removed the Sent copy and reported a plain failure, so a retry sent the
email twice. A send Graph refused outright still cleans its draft up.

Through a real MCP client, with Graph mocked at the transport.
"""

import base64

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import install, module, payload

pytestmark = pytest.mark.requires_connectors

SMALL = {
    "filename": "a.txt",
    "content_base64": base64.b64encode(b"hello").decode(),
    "content_type": "text/plain",
}


def router(send: str):
    """Answer the draft flow; /send times out, is refused, or succeeds."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/createReply") or path.endswith("/createReplyAll"):
            return httpx.Response(201, json={"id": "draft-1"})
        if request.method == "POST" and path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "draft-1"})
        if path.endswith("/send"):
            if send == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if send == "refused":
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(202)
        return httpx.Response(200, json={})

    return handle


def deletes(graph):
    return [r for r in graph if r.method == "DELETE"]


async def call(tool: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(tool, arguments)


SEND = {"to": "b@example.com", "subject": "Hi", "body": "Hello"}
REPLY = {"email_id": "m-1", "body": "Thanks", "attachments": [SMALL]}


@pytest.mark.parametrize(
    "tool, arguments, action",
    [
        ("send_email", SEND, "Failed to send email"),
        ("reply_email", REPLY, "Failed to reply to email"),
        ("send_draft", {"draft_id": "draft-1"}, "Failed to send draft"),
    ],
)
async def test_a_timed_out_send_keeps_the_draft(monkeypatch, tool, arguments, action):
    graph = install(monkeypatch, router("timeout"))

    result = await call(tool, arguments)

    data = payload(result)
    assert data["success"] is False
    assert data["error"].startswith(action)
    assert "Check Sent Items" in data["error"]
    assert deletes(graph) == []


@pytest.mark.parametrize("tool, arguments", [("send_email", SEND), ("reply_email", REPLY)])
async def test_a_refused_send_still_cleans_up(monkeypatch, tool, arguments):
    graph = install(monkeypatch, router("refused"))

    result = await call(tool, arguments)

    data = payload(result)
    assert data["success"] is False
    assert "Check Sent Items" not in data["error"]
    assert [r.url.path for r in deletes(graph)] == ["/v1.0/me/messages/draft-1"]


async def test_a_confirmed_send_is_unchanged(monkeypatch):
    graph = install(monkeypatch, router("ok"))

    result = await call("send_email", SEND)

    assert payload(result)["success"] is True
    assert deletes(graph) == []
