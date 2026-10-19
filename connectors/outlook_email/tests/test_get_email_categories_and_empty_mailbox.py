"""get_email returns a message's categories, and an empty `mailbox` means "omitted".

Two behaviours from the earlier implementation:

- Graph's `categories` (Outlook's colored "Categorize" tags) is requested and
  returned, as an empty list when none is set rather than an absent key.
- `mailbox=""` routes to the signed-in user's own mailbox, exactly like an
  omitted `mailbox`. A caller that normalises a missing value to "" was
  refused with "mailbox must be a valid email address" before the routing
  decision, which already treated "" as omitted, was ever reached.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import install, module, payload

pytestmark = pytest.mark.requires_connectors

MESSAGE = {
    "id": "m-1",
    "subject": "Hello",
    "from": {"emailAddress": {"address": "a@example.com", "name": "A"}},
    "toRecipients": [],
    "receivedDateTime": "2026-09-20T10:00:00Z",
    "body": {"contentType": "text", "content": "hi"},
    "hasAttachments": False,
}


async def get_email(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("get_email", arguments)


async def test_categories_are_requested_and_returned(monkeypatch):
    graph = install(
        monkeypatch, lambda r: httpx.Response(200, json={**MESSAGE, "categories": ["Unassigned"]})
    )

    result = await get_email({"email_id": "m-1"})

    assert result.is_error is False, result.content[0].text
    assert payload(result)["categories"] == ["Unassigned"]
    assert "categories" in graph[0].url.params["$select"].split(",")


async def test_no_categories_is_an_empty_list(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(200, json=MESSAGE))

    result = await get_email({"email_id": "m-1"})

    assert payload(result)["categories"] == []


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("get_email", {"email_id": "m-1"}),
        ("list_attachments", {"email_id": "m-1"}),
        ("read_attachment", {"email_id": "m-1", "attachment_id": "a-1"}),
        ("download_attachment", {"email_id": "m-1", "attachment_id": "a-1"}),
    ],
)
async def test_an_empty_mailbox_reads_the_users_own_mailbox(monkeypatch, tool, arguments):
    graph = install(monkeypatch, lambda r: httpx.Response(200, json={**MESSAGE, "value": []}))

    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, {**arguments, "mailbox": ""})

    text = result.content[0].text
    assert "valid email address" not in text
    assert "allowed_mailboxes" not in text
    assert graph and all(r.url.path.startswith("/v1.0/me/") for r in graph)
    assert graph[0].headers["authorization"] == "Bearer user-token"


async def test_a_named_mailbox_still_goes_app_only(monkeypatch):
    graph = install(monkeypatch, lambda r: httpx.Response(200, json=MESSAGE))

    result = await get_email({"email_id": "m-1", "mailbox": "shared@example.com"})

    assert result.is_error is False, result.content[0].text
    assert graph[0].url.path == "/v1.0/users/shared@example.com/messages/m-1"
    assert graph[0].headers["authorization"] == "Bearer app-token"


async def test_a_malformed_mailbox_is_still_refused(monkeypatch):
    graph = install(monkeypatch, lambda r: httpx.Response(200, json=MESSAGE))

    result = await get_email({"email_id": "m-1", "mailbox": "not-an-address"})

    assert result.is_error is True
    assert "valid email address" in payload(result)["error"]
    assert graph == []
