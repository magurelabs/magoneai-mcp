"""A send whose outcome is unknown keeps its draft and says to check first.

A timeout or a dropped connection on /send, or a 5xx such as a gateway
timeout, leaves the outcome unknown: Graph may have sent the message, and the
draft id is then the sent message. Deleting it removed the Sent copy and
reported a plain failure, so a retry sent the email twice. /sendMail
(save_to_sent=false) has no draft, but gets the same warning. A send that
never reached Graph (a failed connect) or that Graph refused (a 4xx) is a
plain failure and still cleans its draft up.

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
    """Answer the draft flow; /send and /sendMail behave as `send` says."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/createReply") or path.endswith("/createReplyAll"):
            return httpx.Response(201, json={"id": "draft-1"})
        if request.method == "POST" and path.endswith("/me/messages"):
            return httpx.Response(201, json={"id": "draft-1"})
        if path.endswith("/send") or path.endswith("/sendMail"):
            if send == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if send == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if send == "5xx":
                return httpx.Response(504, json={"error": {"message": "gateway timeout"}})
            if send == "refused":
                return httpx.Response(400, json={"error": {"message": "bad recipient"}})
            return httpx.Response(202)
        return httpx.Response(200, json={})

    return handle


def deletes(graph):
    return [r for r in graph if r.method == "DELETE"]


async def call(tool: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(tool, arguments)


SEND = {"to": "b@example.com", "subject": "Hi", "body": "Hello"}
SEND_UNSAVED = {**SEND, "save_to_sent": False}
REPLY = {"email_id": "m-1", "body": "Thanks", "attachments": [SMALL]}
REPLY_SHARED = {**REPLY, "mailbox": "shared@example.com"}


@pytest.mark.parametrize("failure", ["timeout", "5xx"])
@pytest.mark.parametrize(
    "tool, arguments, action",
    [
        ("send_email", SEND, "Failed to send email"),
        ("reply_email", REPLY, "Failed to reply to email"),
        ("reply_email", REPLY_SHARED, "Failed to reply to email"),
        ("send_draft", {"draft_id": "draft-1"}, "Failed to send draft"),
    ],
)
async def test_an_unconfirmed_send_keeps_the_draft(monkeypatch, failure, tool, arguments, action):
    graph = install(monkeypatch, router(failure))

    result = await call(tool, arguments)

    data = payload(result)
    assert data["success"] is False
    assert data["error"].startswith(action)
    assert "Check Sent Items" in data["error"]
    assert deletes(graph) == []


@pytest.mark.parametrize("failure", ["timeout", "5xx"])
async def test_an_unconfirmed_sendmail_warns_without_sent_items(monkeypatch, failure):
    graph = install(monkeypatch, router(failure))

    data = payload(await call("send_email", SEND_UNSAVED))

    assert [r.url.path for r in graph] == ["/v1.0/me/sendMail"]
    assert data["success"] is False
    assert "it may have gone out" in data["error"]
    assert "not saved to Sent Items" in data["error"]


@pytest.mark.parametrize("failure", ["refused", "connect"])
@pytest.mark.parametrize(
    "tool, arguments, deleted",
    [
        ("send_email", SEND, "/v1.0/me/messages/draft-1"),
        ("reply_email", REPLY, "/v1.0/me/messages/draft-1"),
        ("reply_email", REPLY_SHARED, "/v1.0/users/shared@example.com/messages/draft-1"),
    ],
)
async def test_a_failed_send_still_cleans_up(monkeypatch, failure, tool, arguments, deleted):
    graph = install(monkeypatch, router(failure))

    result = await call(tool, arguments)

    data = payload(result)
    assert data["success"] is False
    assert "may have gone out" not in data["error"]
    assert [r.url.path for r in deletes(graph)] == [deleted]


@pytest.mark.parametrize("failure", ["refused", "connect"])
async def test_a_failed_sendmail_is_a_plain_failure(monkeypatch, failure):
    install(monkeypatch, router(failure))

    data = payload(await call("send_email", SEND_UNSAVED))

    assert data["success"] is False
    assert "may have gone out" not in data["error"]


async def test_a_confirmed_send_is_unchanged(monkeypatch):
    graph = install(monkeypatch, router("ok"))

    result = await call("send_email", SEND)

    assert payload(result)["success"] is True
    assert deletes(graph) == []
