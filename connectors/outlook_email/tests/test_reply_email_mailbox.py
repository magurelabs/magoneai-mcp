"""reply_email can reply from a shared mailbox, on both of its paths.

The read tools already reach a shared mailbox app-only (`/users/{mailbox}`)
for flows with no signed-in user, such as a reply to a change notification.
reply_email could not: both its one-call path and its draft-attach-send path
were fixed to `/me`, and a Graph message id belongs to one mailbox, so the
reply 404'd. With `mailbox` the whole sequence, including a large-attachment
upload session and the cleanup of a failed draft, runs against that mailbox.

Through a real MCP client, with Graph mocked at the transport.
"""

import base64

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import body_of, install, module, payload

pytestmark = pytest.mark.requires_connectors

SHARED = "/v1.0/users/shared@example.com"
SMALL = {
    "filename": "a.txt",
    "content_base64": base64.b64encode(b"hello").decode(),
    "content_type": "text/plain",
}


def draft_flow(fail_send: bool = False):
    def router(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/createReply") or path.endswith("/createReplyAll"):
            return httpx.Response(201, json={"id": "draft-1"})
        if path.endswith("/createUploadSession"):
            return httpx.Response(200, json={"uploadUrl": "https://upload.example.com/s/1"})
        if request.url.host == "upload.example.com":
            return httpx.Response(201)
        if path.endswith("/send"):
            return (
                httpx.Response(500, json={"error": {"message": "boom"}})
                if fail_send
                else (httpx.Response(202))
            )
        return httpx.Response(200, json={})

    return router


async def reply(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("reply_email", {"email_id": "m-1", "body": "hi", **arguments})


async def test_the_new_parameter_is_optional():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["reply_email"].input_schema
    assert "mailbox" in schema["properties"]
    assert schema["required"] == ["email_id", "body"]


def flow(graph, base: str = "/v1.0/me", action: str = "createReply"):
    return [(r.method, r.url.path) for r in graph] == [
        ("POST", f"{base}/messages/m-1/{action}"),
        ("PATCH", f"{base}/messages/draft-1"),
        ("POST", f"{base}/messages/draft-1/send"),
    ]


async def test_a_plain_reply_without_mailbox_stays_on_me(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await reply({})

    assert result.is_error is False, result.content[0].text
    assert flow(graph)
    assert all(r.headers["authorization"] == "Bearer user-token" for r in graph)


async def test_an_empty_mailbox_is_the_same_as_none(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await reply({"mailbox": ""})

    assert result.is_error is False, result.content[0].text
    assert flow(graph)


async def test_a_plain_reply_from_a_shared_mailbox_goes_app_only(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await reply({"mailbox": "shared@example.com", "reply_all": True})

    assert result.is_error is False, result.content[0].text
    assert flow(graph, SHARED, "createReplyAll")
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)
    patched = body_of(graph[1])["body"]
    assert patched["contentType"] == "HTML"
    assert ">hi</p>" in patched["content"]


async def test_an_attachment_reply_from_a_shared_mailbox_runs_entirely_there(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await reply({"mailbox": "shared@example.com", "attachments": [SMALL]})

    assert result.is_error is False, result.content[0].text
    assert payload(result)["message_id"] == "draft-1"
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", f"{SHARED}/messages/m-1/createReply"),
        ("PATCH", f"{SHARED}/messages/draft-1"),
        ("POST", f"{SHARED}/messages/draft-1/attachments"),
        ("POST", f"{SHARED}/messages/draft-1/send"),
    ]
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)
    # The sent reply's id is only worth returning if it survives the move to Sent Items.
    assert graph[0].headers["prefer"] == 'IdType="ImmutableId"'


async def test_a_large_attachment_opens_its_upload_session_in_the_shared_mailbox(monkeypatch):
    graph = install(monkeypatch, draft_flow())
    large = {
        "filename": "big.bin",
        "content_base64": base64.b64encode(b"x" * (3 * 1024 * 1024 + 10)).decode(),
        "content_type": "application/octet-stream",
    }

    result = await reply({"mailbox": "shared@example.com", "attachments": [large]})

    assert result.is_error is False, result.content[0].text
    session = next(r for r in graph if r.url.path.endswith("/createUploadSession"))
    assert session.url.path == f"{SHARED}/messages/draft-1/attachments/createUploadSession"
    assert session.headers["authorization"] == "Bearer app-token"
    upload = next(r for r in graph if r.url.host == "upload.example.com")
    assert "authorization" not in upload.headers


async def test_a_failed_send_cleans_up_the_draft_in_the_shared_mailbox(monkeypatch):
    graph = install(monkeypatch, draft_flow(fail_send=True))

    result = await reply({"mailbox": "shared@example.com", "attachments": [SMALL]})

    assert result.is_error is True
    assert (graph[-1].method, graph[-1].url.path) == ("DELETE", f"{SHARED}/messages/draft-1")
    assert graph[-1].headers["authorization"] == "Bearer app-token"


async def test_an_attachment_reply_without_mailbox_stays_on_me(monkeypatch):
    graph = install(monkeypatch, draft_flow())

    result = await reply({"attachments": [SMALL]})

    assert result.is_error is False, result.content[0].text
    assert all(r.url.path.startswith("/v1.0/me/") for r in graph)
    assert all(r.headers["authorization"] == "Bearer user-token" for r in graph)


@pytest.mark.parametrize(
    "mailbox,expected",
    [("not-an-address", "valid email address"), ("other@example.com", "not permitted")],
)
async def test_a_bad_or_unlisted_mailbox_is_refused_before_graph(monkeypatch, mailbox, expected):
    graph = install(monkeypatch, draft_flow())

    result = await reply({"mailbox": mailbox, "attachments": [SMALL]})

    assert result.is_error is True
    assert expected in payload(result)["error"]
    assert graph == []
