"""Replies keep their paragraphs, and a reply can be left in Drafts.

The one-call /reply took the new text as a plain-text `comment`, and Outlook
drops its line breaks inside an HTML thread: a five-paragraph reply reached
the recipient as one block. Every reply now goes through createReply, with
the text written as HTML above the quoted thread. create_draft with
`reply_to_id` builds the same reply and leaves it in Drafts.

Through a real MCP client, with Graph mocked at the transport.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import body_of, install, module, payload

pytestmark = pytest.mark.requires_connectors

QUOTED = '<html><head></head><body><div id="divRplyFwdMsg">From: A</div></body></html>'
WEB_LINK = "https://outlook.example.com/draft-1"


def reply_flow(created_cc: list | None = None, created_to: list | None = None):
    def router(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/createReply") or path.endswith("/createReplyAll"):
            return httpx.Response(
                201,
                json={
                    "id": "draft-1",
                    "webLink": WEB_LINK,
                    "body": {"contentType": "html", "content": QUOTED},
                    "ccRecipients": created_cc or [],
                    "toRecipients": created_to or [],
                },
            )
        if path.endswith("/send"):
            return httpx.Response(202)
        return httpx.Response(200, json={})

    return router


async def call(tool: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(tool, arguments)


def patch_of(graph) -> dict:
    return body_of(next(r for r in graph if r.method == "PATCH"))


def address(email: str) -> dict:
    return {"emailAddress": {"address": email}}


def test_text_to_html_keeps_paragraphs_and_line_breaks():
    html = module.text_to_html("One\nline two\n\n  \nPara <2> & more")

    assert html.count("<p ") == 2
    assert "One<br>line two" in html
    assert "Para &lt;2&gt; &amp; more" in html


def test_reply_body_goes_inside_the_body_above_the_thread():
    content = module.reply_body("Hello", False, {"contentType": "html", "content": QUOTED})[
        "content"
    ]

    assert content.index("Hello") < content.index("divRplyFwdMsg")
    assert content.index("<body>") < content.index("Hello")


def test_a_plain_text_thread_is_converted_to_html():
    body = module.reply_body("<b>Hi</b>", True, {"contentType": "text", "content": "a\n\nb"})

    assert body["contentType"] == "HTML"
    assert body["content"].startswith('<div style="')
    assert "<b>Hi</b>" in body["content"]
    assert body["content"].count("<p ") == 2


async def test_reply_email_sends_paragraphs_and_returns_the_id(monkeypatch):
    graph = install(monkeypatch, reply_flow())

    result = await call("reply_email", {"email_id": "m-1", "body": "First.\n\nSecond."})

    data = payload(result)
    assert data["success"] is True
    assert data["message_id"] == "draft-1"
    content = patch_of(graph)["body"]["content"]
    assert content.count("<p ") == 2
    assert content.index("Second.") < content.index("divRplyFwdMsg")
    assert graph[-1].url.path == "/v1.0/me/messages/draft-1/send"


async def test_extra_cc_is_added_to_the_reply_all_cc(monkeypatch):
    graph = install(monkeypatch, reply_flow(created_cc=[address("x@example.com")]))

    await call(
        "reply_email",
        {"email_id": "m-1", "body": "Hi", "reply_all": True, "cc": "y@example.com, X@example.com"},
    )

    cc = [r["emailAddress"]["address"] for r in patch_of(graph)["ccRecipients"]]
    assert cc == ["x@example.com", "y@example.com"]


async def test_create_draft_with_reply_to_id_leaves_the_reply_in_drafts(monkeypatch):
    graph = install(monkeypatch, reply_flow())

    result = await call(
        "create_draft",
        {"to": "a@example.com", "subject": "RE: Hi", "body": "Draft text", "reply_to_id": "m-1"},
    )

    data = payload(result)
    assert data["success"] is True
    assert data["draft_id"] == "draft-1"
    assert data["web_link"] == WEB_LINK
    assert data["message"] == "Reply draft created"
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", "/v1.0/me/messages/m-1/createReply"),
        ("PATCH", "/v1.0/me/messages/draft-1"),
    ]
    patch = patch_of(graph)
    assert patch["subject"] == "RE: Hi"
    assert "Draft text" in patch["body"]["content"]


async def test_create_draft_reply_all_uses_create_reply_all(monkeypatch):
    graph = install(monkeypatch, reply_flow())

    await call(
        "create_draft",
        {
            "to": "a@example.com",
            "subject": "s",
            "body": "b",
            "reply_to_id": "m-1",
            "reply_all": True,
        },
    )

    assert graph[0].url.path == "/v1.0/me/messages/m-1/createReplyAll"


async def test_create_draft_reply_all_keeps_everyone_on_to(monkeypatch):
    graph = install(monkeypatch, reply_flow(created_to=[address("alice@example.com")]))

    await call(
        "create_draft",
        {
            "to": "bob@example.com, ALICE@example.com",
            "subject": "RE: Hi",
            "body": "b",
            "reply_to_id": "m-1",
            "reply_all": True,
        },
    )

    to = [r["emailAddress"]["address"] for r in patch_of(graph)["toRecipients"]]
    assert to == ["alice@example.com", "bob@example.com"]


async def test_create_draft_rejects_a_bad_reply_to_id(monkeypatch):
    graph = install(monkeypatch, reply_flow())

    result = await call(
        "create_draft", {"to": "a@example.com", "subject": "s", "body": "b", "reply_to_id": "a/b"}
    )

    assert payload(result)["success"] is False
    assert graph == []


async def test_a_new_draft_returns_its_web_link(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(201, json={"id": "draft-2", "webLink": WEB_LINK}))

    result = await call("create_draft", {"to": "a@example.com", "subject": "s", "body": "b"})

    assert payload(result)["web_link"] == WEB_LINK
