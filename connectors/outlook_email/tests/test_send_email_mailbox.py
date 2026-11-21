"""send_email can send AS a shared mailbox, on both of its paths.

A mail with no inbound message to reply to (an automated acknowledgment for a
ticket raised by phone or in person) cannot use reply_email, so it went out
from the connection's signed-in account whatever the caller wanted. With
`mailbox` the create-draft/send (or sendMail) sequence runs app-only against
that mailbox, after the connection's allowed_mailboxes check.

Through a real MCP client, with Graph mocked at the transport.
"""

import base64

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import HEADERS, body_of, install, module, payload

pytestmark = pytest.mark.requires_connectors

SHARED = "/v1.0/users/shared@example.com"
BASE = {"to": "a@example.com", "subject": "Hello", "body": "hi"}
SMALL = {
    "filename": "a.txt",
    "content_base64": base64.b64encode(b"hello").decode(),
    "content_type": "text/plain",
}
LARGE = {
    "filename": "big.bin",
    "content_base64": base64.b64encode(b"x" * (3 * 1024 * 1024 + 10)).decode(),
    "content_type": "application/octet-stream",
}


def draft_flow(fail_send: bool = False):
    def router(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/messages"):
            return httpx.Response(201, json={"id": "draft-1"})
        if path.endswith("/send"):
            return (
                httpx.Response(500, json={"error": {"message": "boom"}})
                if fail_send
                else (httpx.Response(202))
            )
        return httpx.Response(202)

    return router


async def send(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("send_email", {**BASE, **arguments})


async def test_the_new_parameter_is_optional():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["send_email"].input_schema
    assert "mailbox" in schema["properties"]
    assert schema["required"] == ["to", "subject", "body"]


@pytest.mark.parametrize("mailbox", [None, "", "   "])
async def test_without_a_mailbox_it_stays_on_me(monkeypatch, mailbox):
    graph = install(monkeypatch, draft_flow())

    result = await send({} if mailbox is None else {"mailbox": mailbox})

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
    ]
    assert all(r.headers["authorization"] == "Bearer user-token" for r in graph)
    assert "mailbox" not in payload(result)


async def test_a_shared_mailbox_send_goes_app_only_and_keeps_the_immutable_id(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await send({"mailbox": "shared@example.com"})

    assert result.is_error is False, result.content[0].text
    assert payload(result)["message_id"] == "draft-1"
    assert payload(result)["mailbox"] == "shared@example.com"
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", f"{SHARED}/messages"),
        ("POST", f"{SHARED}/messages/draft-1/send"),
    ]
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)
    assert graph[0].headers["prefer"] == 'IdType="ImmutableId"'
    assert body_of(graph[0])["subject"] == "Hello"


async def test_without_save_to_sent_a_shared_mailbox_uses_sendmail(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await send({"mailbox": "shared@example.com", "save_to_sent": False})

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [("POST", f"{SHARED}/sendMail")]
    assert graph[0].headers["authorization"] == "Bearer app-token"
    assert body_of(graph[0])["saveToSentItems"] is False


async def test_a_small_attachment_rides_along_in_the_shared_mailbox_draft(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await send({"mailbox": "shared@example.com", "attachments": [SMALL]})

    assert result.is_error is False, result.content[0].text
    assert body_of(graph[0])["attachments"][0]["name"] == "a.txt"


async def test_a_large_attachment_is_refused_from_a_shared_mailbox_before_graph(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await send({"mailbox": "shared@example.com", "attachments": [LARGE]})

    assert payload(result)["success"] is False
    assert "shared mailbox" in payload(result)["error"]
    assert graph == []


async def test_a_failed_shared_send_deletes_the_orphan_draft_there(monkeypatch):
    graph = install(monkeypatch, draft_flow(fail_send=True))

    result = await send({"mailbox": "shared@example.com"})

    assert payload(result)["success"] is False
    assert (graph[-1].method, graph[-1].url.path) == ("DELETE", f"{SHARED}/messages/draft-1")
    assert graph[-1].headers["authorization"] == "Bearer app-token"


@pytest.mark.parametrize(
    "mailbox,expected",
    [("not-an-address", "valid email address"), ("other@example.com", "not permitted")],
)
async def test_a_bad_or_unlisted_mailbox_is_refused_before_graph(monkeypatch, mailbox, expected):
    graph = install(monkeypatch, draft_flow())

    result = await send({"mailbox": mailbox})

    assert payload(result)["success"] is False
    assert expected in payload(result)["error"]
    assert graph == []


async def test_no_allowlist_means_no_shared_mailbox_send(monkeypatch):
    headers = {k: v for k, v in HEADERS.items() if k != "x-allowed-mailboxes"}
    graph = install(monkeypatch, draft_flow(), headers=headers)

    result = await send({"mailbox": "shared@example.com"})

    assert payload(result)["success"] is False
    assert graph == []
