"""read_attachment reads an attached email: its headers and its body.

An email attached in Outlook is an itemAttachment with no contentBytes, so it
used to decode to nothing and come back as "not text-extractable": the agent
could not see who an attached message went to. It is now read through
$expand. An .eml or message/rfc822 file attachment reads the same way.

Through a real MCP client, with Graph mocked at the transport.
"""

import base64

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import install, module, payload

pytestmark = pytest.mark.requires_connectors

ITEM_META = {
    "@odata.type": "#microsoft.graph.itemAttachment",
    "id": "att-1",
    "name": "FW: Notice",
    "contentType": None,
    # An item's size counts its own attachments, which are never fetched here.
    "size": 900_000_000,
}
ITEM = {
    "@odata.type": "#microsoft.graph.message",
    "subject": "FW: Notice",
    "sentDateTime": "2026-01-02T03:04:05Z",
    "from": {"emailAddress": {"name": "Sender One", "address": "one@example.com"}},
    "toRecipients": [
        {"emailAddress": {"name": "Team A", "address": "a@example.com"}},
        {"emailAddress": {"name": "b@example.com", "address": "b@example.com"}},
    ],
    "ccRecipients": [{"emailAddress": {"name": "Lead", "address": "lead@example.com"}}],
    "body": {"contentType": "html", "content": "<p>Please note&nbsp;the notice.</p>"},
}

EML = (
    b"From: Sender One <one@example.com>\r\n"
    b"To: Team A <a@example.com>, b@example.com\r\n"
    b"Cc: Lead <lead@example.com>\r\n"
    b"Subject: FW: Notice\r\n"
    b"Date: Fri, 02 Jan 2026 07:04:05 +0400\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="b1"\r\n\r\n'
    b"--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Html body</p>\r\n"
    b"--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain body\r\n"
    b"--b1--\r\n"
)


def attachment(meta: dict, full: dict):
    """Answer the $select metadata read, then the full (or $expand) read."""

    def router(request: httpx.Request) -> httpx.Response:
        if "$select" in request.url.params:
            return httpx.Response(200, json=meta)
        return httpx.Response(200, json=full)

    return router


async def read(attachment_id: str = "att-1"):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(
            "read_attachment", {"email_id": "m-1", "attachment_id": attachment_id}
        )


async def test_an_attached_item_returns_its_headers_and_body(monkeypatch):
    graph = install(monkeypatch, attachment(ITEM_META, {**ITEM_META, "item": ITEM}))

    data = payload(await read())

    assert data["success"] is True
    assert data["content_type"] == "message/rfc822"
    text = data["text"]
    assert "From: Sender One <one@example.com>" in text
    assert "To: Team A <a@example.com>, b@example.com" in text
    assert "Cc: Lead <lead@example.com>" in text
    assert "Subject: FW: Notice" in text
    assert "Please note the notice." in text
    assert graph[-1].url.params["$expand"] == "microsoft.graph.itemattachment/item"


@pytest.mark.parametrize("name, content_type", [("fwd.eml", ""), ("attached", "message/rfc822")])
async def test_an_eml_file_returns_its_headers_and_plain_body(monkeypatch, name, content_type):
    meta = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "att-2",
        "name": name,
        "contentType": content_type,
        "size": len(EML),
    }
    full = {**meta, "contentBytes": base64.b64encode(EML).decode()}
    install(monkeypatch, attachment(meta, full))

    text = payload(await read("att-2"))["text"]

    assert "From: Sender One <one@example.com>" in text
    assert "To: Team A <a@example.com>, b@example.com" in text
    assert "Cc: Lead <lead@example.com>" in text
    assert "Plain body" in text
    assert "Html body" not in text


def test_an_html_only_eml_is_stripped_to_text():
    eml = b"From: x@example.com\r\nSubject: s\r\nContent-Type: text/html\r\n\r\n<div>A&amp;B</div>"

    assert module.extract_text("m.eml", eml).endswith("A&B")


def test_an_eml_in_an_unknown_charset_still_reads():
    eml = (
        b"From: x@example.com\r\nTo: y@example.com\r\nSubject: s\r\n"
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n\r\nHello caf\xc3\xa9"
    )

    text = module.extract_text("m.eml", eml)

    assert "To: y@example.com" in text
    assert text.endswith("Hello caf\u00e9")


async def test_a_file_attachment_is_unchanged(monkeypatch):
    meta = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "att-3",
        "name": "a.txt",
        "contentType": "text/plain",
        "size": 5,
    }
    full = {**meta, "contentBytes": base64.b64encode(b"hello").decode()}
    install(monkeypatch, attachment(meta, full))

    data = payload(await read("att-3"))

    assert data["success"] is True
    assert data["text"] == "hello"
