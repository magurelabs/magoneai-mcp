"""send_email, forward_email and mark_emails can act as a shared mailbox.

Same app-only model reply_email already has: with `mailbox` the call goes to
`/users/{mailbox}` using the app token, after the mailbox passes the
connection's allowed_mailboxes check. Omitted, every tool stays on the
delegated `/me` path exactly as before. forward_email additionally builds a
full forward through createForward when any override is given.

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
LARGE = {
    "filename": "big.bin",
    "content_base64": base64.b64encode(b"x" * (3 * 1024 * 1024 + 10)).decode(),
    "content_type": "application/octet-stream",
}


def router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith(("/createForward", "/createReply")) or path.endswith("/messages"):
        return httpx.Response(201, json={"id": "draft-1"})
    if path.endswith("/attachments") and request.method == "GET":
        return httpx.Response(200, json={"value": [{"id": "orig-att"}]})
    if path.endswith("/send") or path.endswith("/sendMail") or path.endswith("/forward"):
        return httpx.Response(202)
    return httpx.Response(200, json={})


async def call(name: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(name, arguments)


SEND = {"to": "a@example.com", "subject": "s", "body": "b"}


async def test_send_without_mailbox_stays_on_me(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call("send_email", SEND)

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
    ]
    assert all(r.headers["authorization"] == "Bearer user-token" for r in graph)


async def test_send_from_a_shared_mailbox_goes_app_only(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call("send_email", {**SEND, "mailbox": "shared@example.com"})

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", f"{SHARED}/messages"),
        ("POST", f"{SHARED}/messages/draft-1/send"),
    ]
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)
    assert payload(result)["mailbox"] == "shared@example.com"


async def test_send_from_a_shared_mailbox_without_saving_uses_sendmail(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "send_email", {**SEND, "mailbox": "shared@example.com", "save_to_sent": False}
    )

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == [f"{SHARED}/sendMail"]
    assert body_of(graph[0])["saveToSentItems"] is False


async def test_an_empty_send_mailbox_is_the_same_as_none(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call("send_email", {**SEND, "mailbox": "  "})

    assert result.is_error is False, result.content[0].text
    assert all(r.url.path.startswith("/v1.0/me/") for r in graph)


async def test_a_large_attachment_is_refused_from_a_shared_mailbox(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "send_email", {**SEND, "mailbox": "shared@example.com", "attachments": [LARGE]}
    )

    assert result.is_error is True
    assert "shared mailbox" in payload(result)["error"]
    assert graph == []


async def test_a_failed_shared_send_cleans_up_its_draft(monkeypatch):
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/send"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return router(request)

    graph = install(monkeypatch, failing)

    result = await call("send_email", {**SEND, "mailbox": "shared@example.com"})

    assert result.is_error is True
    assert (graph[-1].method, graph[-1].url.path) == ("DELETE", f"{SHARED}/messages/draft-1")
    assert graph[-1].headers["authorization"] == "Bearer app-token"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("send_email", SEND),
        ("forward_email", {"email_id": "m-1", "to": "a@example.com"}),
        ("mark_emails", {"email_ids": ["m-1"], "is_read": True}),
    ],
)
@pytest.mark.parametrize(
    "mailbox,expected",
    [("not-an-address", "valid email address"), ("other@example.com", "not permitted")],
)
async def test_a_bad_or_unlisted_mailbox_is_refused_before_graph(
    monkeypatch, tool, args, mailbox, expected
):
    graph = install(monkeypatch, router)

    result = await call(tool, {**args, "mailbox": mailbox})

    assert result.is_error is True
    assert expected in payload(result)["error"]
    assert graph == []


async def test_a_plain_forward_is_the_one_call_action(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "forward_email", {"email_id": "m-1", "to": "a@example.com", "comment": "fyi"}
    )

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == ["/v1.0/me/messages/m-1/forward"]
    assert body_of(graph[0])["comment"] == "fyi"


async def test_a_full_forward_builds_a_draft_and_drops_the_original_files(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "forward_email",
        {
            "email_id": "m-1",
            "to": "a@example.com",
            "cc": "c@example.com",
            "subject": "Fwd: x",
            "body": "<p>note</p>",
            "is_html": True,
            "attachments": [SMALL],
        },
    )

    assert result.is_error is False, result.content[0].text
    assert [(r.method, r.url.path) for r in graph] == [
        ("POST", "/v1.0/me/messages/m-1/createForward"),
        ("PATCH", "/v1.0/me/messages/draft-1"),
        ("GET", "/v1.0/me/messages/draft-1/attachments"),
        ("DELETE", "/v1.0/me/messages/draft-1/attachments/orig-att"),
        ("POST", "/v1.0/me/messages/draft-1/attachments"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
    ]
    patch = body_of(graph[1])
    assert patch["subject"] == "Fwd: x"
    assert patch["body"] == {"contentType": "HTML", "content": "<p>note</p>"}
    assert patch["ccRecipients"][0]["emailAddress"]["address"] == "c@example.com"


async def test_keep_original_attachments_leaves_the_copied_files(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "forward_email",
        {
            "email_id": "m-1",
            "to": "a@example.com",
            "body": "note",
            "keep_original_attachments": True,
        },
    )

    assert result.is_error is False, result.content[0].text
    assert not any(r.method == "DELETE" for r in graph)


async def test_a_forward_from_a_shared_mailbox_runs_entirely_there(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "forward_email",
        {"email_id": "m-1", "to": "a@example.com", "mailbox": "shared@example.com"},
    )

    assert result.is_error is False, result.content[0].text
    assert graph[0].url.path == f"{SHARED}/messages/m-1/createForward"
    assert all(r.url.path.startswith(SHARED) for r in graph)
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)


async def test_comment_cannot_be_combined_with_a_draft_override(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "forward_email",
        {"email_id": "m-1", "to": "a@example.com", "comment": "fyi", "body": "x"},
    )

    assert result.is_error is True
    assert "comment cannot be combined" in payload(result)["error"]
    assert graph == []


async def test_mark_without_mailbox_stays_on_me(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call("mark_emails", {"email_ids": ["m-1", "m-2"], "is_read": True})

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == ["/v1.0/me/messages/m-1", "/v1.0/me/messages/m-2"]
    assert all(body_of(r) == {"isRead": True} for r in graph)


async def test_mark_in_a_shared_mailbox_goes_app_only(monkeypatch):
    graph = install(monkeypatch, router)

    result = await call(
        "mark_emails", {"email_ids": ["m-1"], "is_read": False, "mailbox": "shared@example.com"}
    )

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == [f"{SHARED}/messages/m-1"]
    assert graph[0].headers["authorization"] == "Bearer app-token"
