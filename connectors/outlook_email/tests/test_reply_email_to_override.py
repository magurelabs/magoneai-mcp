"""reply_email's `to` replaces the default recipient and keeps the thread.

Graph can only change a reply's To by patching a createReply draft, so `to`
forces the draft path even with no attachments, and it overrides reply_all
(a draft's To is rewritten wholesale).

Through a real MCP client, with Graph mocked at the transport.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import body_of, install, module, payload

pytestmark = pytest.mark.requires_connectors

SHARED = "/v1.0/users/shared@example.com"


def draft_flow(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith(("/createReply", "/createReplyAll")):
        return httpx.Response(201, json={"id": "draft-1"})
    if request.url.path.endswith("/send"):
        return httpx.Response(202)
    return httpx.Response(200, json={})


async def reply(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("reply_email", {"email_id": "m-1", "body": "hi", **arguments})


async def test_the_parameter_is_optional_and_advertised():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["reply_email"].input_schema
    assert "to" in schema["properties"]
    assert schema["required"] == ["email_id", "body"]


async def test_to_patches_the_draft_recipients_and_sends(monkeypatch):
    graph = install(monkeypatch, draft_flow)

    result = await reply({"to": "a@example.com, b@example.com", "is_html": True})

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", "/v1.0/me/messages/m-1/createReply"),
        ("PATCH", "/v1.0/me/messages/draft-1"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
    ]
    patch = body_of(graph[1])
    assert [r["emailAddress"]["address"] for r in patch["toRecipients"]] == [
        "a@example.com",
        "b@example.com",
    ]
    assert patch["body"] == {"contentType": "HTML", "content": "hi"}
    assert payload(result)["message_id"] == "draft-1"


async def test_to_overrides_reply_all(monkeypatch):
    graph = install(monkeypatch, draft_flow)

    result = await reply({"to": "a@example.com", "reply_all": True})

    assert result.is_error is False, result.content[0].text
    assert graph[0].url.path.endswith("/createReply")
    assert payload(result)["reply_all"] is False


async def test_to_keeps_cc_and_bcc(monkeypatch):
    graph = install(monkeypatch, draft_flow)

    await reply({"to": "a@example.com", "cc": "c@example.com", "bcc": "d@example.com"})

    patch = body_of(graph[1])
    assert patch["ccRecipients"][0]["emailAddress"]["address"] == "c@example.com"
    assert patch["bccRecipients"][0]["emailAddress"]["address"] == "d@example.com"


async def test_to_runs_in_a_shared_mailbox(monkeypatch):
    graph = install(monkeypatch, draft_flow)

    result = await reply({"to": "a@example.com", "mailbox": "shared@example.com"})

    assert result.is_error is False, result.content[0].text
    assert all(r.url.path.startswith(SHARED) for r in graph)
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)


@pytest.mark.parametrize("to", ["", "   "])
async def test_an_empty_to_keeps_the_one_call_reply(monkeypatch, to):
    graph = install(monkeypatch, lambda r: httpx.Response(202))

    result = await reply({"to": to})

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == ["/v1.0/me/messages/m-1/reply"]


async def test_an_invalid_to_is_refused_before_graph(monkeypatch):
    graph = install(monkeypatch, draft_flow)

    result = await reply({"to": "not-an-address"})

    assert result.is_error is True
    assert graph == []
